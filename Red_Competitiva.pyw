import base64
import ctypes
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import unicodedata
from tkinter import messagebox, ttk


CREATE_NO_WINDOW = 0x08000000
APP_NAME = "RedCompetitiva"
BACKUP_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME, "ResetClean")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def powershell(script, input_text=None, timeout=45):
    """Run a built-in NetAdapter query/change without opening a console."""
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false;"
        + script
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=timeout,
    )
    if result.returncode:
        message = (result.stderr or result.stdout or "PowerShell terminó con error.").strip()
        raise RuntimeError(message)
    return result.stdout.strip()


def powershell_json(script, input_text=None, timeout=45):
    output = powershell(script, input_text=input_text, timeout=timeout)
    if not output:
        return []
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Windows devolvió una respuesta que no se pudo leer: {}".format(output[:400])) from exc
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def run_command(args, timeout=60):
    """Run a console tool (netsh, reg, ipconfig) silently and return (ok, short detail)."""
    try:
        result = subprocess.run(
            args,
            shell=False,
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "No se encontró el comando: {}".format(args[0])
    except subprocess.TimeoutExpired:
        return False, "Tiempo agotado: {}".format(" ".join(args))
    detail = (result.stderr or result.stdout or "").strip()
    detail = " ".join(detail.split())[:300]
    return result.returncode == 0, detail or ("Correcto" if result.returncode == 0 else "código {}".format(result.returncode))


def domain_joined():
    try:
        return powershell("(Get-CimInstance Win32_ComputerSystem).PartOfDomain", timeout=30).strip().lower() == "true"
    except Exception:
        return False


def list_adapters():
    # -Physical still exposes Bluetooth PAN and virtual NDIS devices; exclude
    # those so the combo box only shows real network cards.
    script = r'''
$rows = @(Get-NetAdapter -Physical |
    Where-Object { $_.InterfaceDescription -notmatch 'Bluetooth|Virtual|TAP|TUN|WAN Miniport' } |
    ForEach-Object {
        [pscustomobject]@{
            Name = [string]$_.Name
            Description = [string]$_.InterfaceDescription
            Status = [string]$_.Status
            LinkSpeed = [string]$_.LinkSpeed
            Guid = [string]$_.InterfaceGuid
        }
    })
ConvertTo-Json -InputObject $rows -Depth 4 -Compress
'''
    return powershell_json(script)


def get_properties(adapter_name):
    payload = base64.b64encode(adapter_name.encode("utf-8")).decode("ascii")
    script = r'''
$name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))
$rows = @(Get-NetAdapterAdvancedProperty -Name $name -AllProperties | ForEach-Object {
    [pscustomobject]@{
        DisplayName = [string]$_.DisplayName
        DisplayValue = [string]$_.DisplayValue
        ValidDisplayValues = @($_.ValidDisplayValues)
        NumericParameterMinValue = [string]$_.NumericParameterMinValue
        NumericParameterMaxValue = [string]$_.NumericParameterMaxValue
        NumericParameterStepValue = [string]$_.NumericParameterStepValue
        RegistryKeyword = [string]$_.RegistryKeyword
        RegistryValue = $_.RegistryValue
        ValidRegistryValues = @($_.ValidRegistryValues)
    }
})
ConvertTo-Json -InputObject $rows -Depth 6 -Compress
'''.replace("__PAYLOAD__", payload)
    return powershell_json(script)


def get_rss_state(adapter_name):
    payload = base64.b64encode(adapter_name.encode("utf-8")).decode("ascii")
    script = r'''
$name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))
$rss = @(Get-NetAdapterRss -Name $name -ErrorAction Stop | ForEach-Object {
    [pscustomobject]@{ Enabled=[bool]$_.Enabled }
})
ConvertTo-Json -InputObject $rss -Depth 3 -Compress
'''.replace("__PAYLOAD__", payload)
    try:
        rows = powershell_json(script)
        return bool(rows[0].get("Enabled")) if rows else None
    except Exception:
        return None


def get_power_management_state(adapter_name):
    payload = base64.b64encode(adapter_name.encode("utf-8")).decode("ascii")
    script = r'''
$name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))
$rows = @(Get-NetAdapterPowerManagement -Name $name -ErrorAction Stop | Select-Object -First 1 | ForEach-Object {
    [pscustomobject]@{
        SelectiveSuspend = [string]$_.SelectiveSuspend
        DeviceSleepOnDisconnect = [string]$_.DeviceSleepOnDisconnect
        D0PacketCoalescing = [string]$_.D0PacketCoalescing
    }
})
ConvertTo-Json -InputObject $rows -Depth 3 -Compress
'''.replace("__PAYLOAD__", payload)
    try:
        rows = powershell_json(script)
        if not rows:
            return None
        state = rows[0]
        return state if any(state.values()) else None
    except Exception:
        return None


def restart_adapter(adapter_name, timeout=60):
    payload = base64.b64encode(adapter_name.encode("utf-8")).decode("ascii")
    script = r'''
$name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))
Restart-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop | Out-Null
'''.replace("__PAYLOAD__", payload)
    powershell(script, timeout=timeout)


def set_properties(adapter_name, changes, rss_enabled=None, power_management=None,
                   reset_advanced=False, restart=True):
    payload = json.dumps({
        "adapter": adapter_name, "changes": changes, "rssEnabled": rss_enabled,
        "powerManagement": power_management, "resetAdvanced": reset_advanced,
    }, ensure_ascii=False)
    script = r'''
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
$name = [string]$data.adapter
$results = @()
$appliedCount = 0
if ($data.resetAdvanced) {
    try {
        Reset-NetAdapterAdvancedProperty -Name $name -DisplayName '*' -NoRestart -Confirm:$false -ErrorAction Stop
        $appliedCount++
        $results += [pscustomobject]@{Property='Propiedades avanzadas'; Success=$true; Detail='Restauradas a valores predeterminados del controlador'}
    } catch {
        $results += [pscustomobject]@{Property='Restaurar valores del controlador'; Success=$false; Detail=$_.Exception.Message}
    }
}
$validKeywords = @(Get-NetAdapterAdvancedProperty -Name $name -AllProperties -ErrorAction Stop |
    ForEach-Object { [string]$_.RegistryKeyword })
foreach ($change in @($data.changes)) {
    $keyword = [string]$change.RegistryKeyword
    if ($keyword -eq 'ASPM' -or $validKeywords -notcontains $keyword) {
        # Ignore stale saved keys without reporting a false driver error.
        $results += [pscustomobject]@{Property=[string]$change.Label; Success=$true; Skipped=$true; Detail='Omitido: esta propiedad no existe en el adaptador actual'}
        continue
    }
    try {
        if ($change.Restore -or $change.UseRegistryValue) {
            $values = @($change.RegistryValue | ForEach-Object { [string]$_ })
            Set-NetAdapterAdvancedProperty -Name $name -RegistryKeyword ([string]$change.RegistryKeyword) -RegistryValue $values -NoRestart -ErrorAction Stop
        } else {
            Set-NetAdapterAdvancedProperty -Name $name -RegistryKeyword ([string]$change.RegistryKeyword) -DisplayValue ([string]$change.DisplayValue) -NoRestart -ErrorAction Stop
        }
        $appliedCount++
        $results += [pscustomobject]@{Property=[string]$change.Label; Success=$true; Detail='Aplicado'}
    } catch {
        $results += [pscustomobject]@{Property=[string]$change.Label; Success=$false; Detail=$_.Exception.Message}
    }
}
if ($null -ne $data.powerManagement) {
    $mode = [string]$data.powerManagement.Mode
    $fields = @('SelectiveSuspend', 'DeviceSleepOnDisconnect', 'D0PacketCoalescing')
    if ($mode -eq 'enable' -or $mode -eq 'disable') {
        # Apply each feature on its own: most wired NICs do not support
        # SelectiveSuspend and the all-in-one cmdlet would fail entirely.
        $desired = if ($mode -eq 'enable') { 'Enabled' } else { 'Disabled' }
        foreach ($field in $fields) {
            try {
                $oneArgs = @{Name=$name; NoRestart=$true; ErrorAction='Stop'}
                $oneArgs[$field] = $desired
                Set-NetAdapterPowerManagement @oneArgs | Out-Null
                $appliedCount++
                $results += [pscustomobject]@{Property=('Ahorro de energía: ' + $field); Success=$true; Detail=$desired}
            } catch {
                $results += [pscustomobject]@{Property=('Ahorro de energía: ' + $field); Success=$true; Skipped=$true; Detail='No soportado por este adaptador'}
            }
        }
    } elseif ($mode -eq 'restore') {
        try {
            $pmArgs = @{Name=$name; NoRestart=$true; ErrorAction='Stop'}
            $state = $data.powerManagement.State
            foreach ($field in $fields) {
                $value = [string]$state.$field
                if ($value -in @('Enabled', 'Disabled')) { $pmArgs[$field] = $value }
            }
            if ($pmArgs.Count -gt 3) {
                Set-NetAdapterPowerManagement @pmArgs | Out-Null
                $appliedCount++
                $results += [pscustomobject]@{Property='Ahorro de energía del adaptador'; Success=$true; Detail='Estado original restaurado'}
            } else {
                $results += [pscustomobject]@{Property='Ahorro de energía del adaptador'; Success=$true; Skipped=$true; Detail='El adaptador no expuso valores que se puedan restaurar'}
            }
        } catch {
            $results += [pscustomobject]@{Property='Ahorro de energía del adaptador'; Success=$false; Detail=$_.Exception.Message}
        }
    }
}
if (__RESTART__ -and ($appliedCount -gt 0 -or $null -ne $data.rssEnabled)) {
    if ($null -ne $data.rssEnabled) {
        try {
            Set-NetAdapterRss -Name $name -Enabled ([bool]$data.rssEnabled) -ErrorAction Stop | Out-Null
            $results += [pscustomobject]@{Property='RSS de Windows'; Success=$true; Detail='Estado aplicado'}
        } catch {
            $results += [pscustomobject]@{Property='RSS de Windows'; Success=$false; Detail=$_.Exception.Message}
        }
    }
    if ($appliedCount -gt 0) {
        try {
            Restart-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop | Out-Null
            $results += [pscustomobject]@{Property='Adaptador'; Success=$true; Detail='Reiniciado para aplicar los cambios'}
        } catch {
            $results += [pscustomobject]@{Property='Reinicio del adaptador'; Success=$false; Detail=$_.Exception.Message}
        }
    }
}
ConvertTo-Json -InputObject @($results) -Depth 5 -Compress
'''.replace("__RESTART__", "$true" if restart else "$false")
    return powershell_json(script, input_text=payload, timeout=180)


DEFAULT_NETWORK_SERVICES = {
    "BFE": "Automatic",
    "Dhcp": "Automatic",
    "Dnscache": "Automatic",
    "NlaSvc": "Automatic",
    "mpssvc": "Automatic",
    "Wcmsvc": "Automatic",
    "iphlpsvc": "Automatic",
    "LanmanWorkstation": "Automatic",
    "LanmanServer": "Automatic",
    "WlanSvc": "Automatic",
    "netprofm": "Manual",
    "WinHttpAutoProxySvc": "Manual",
    "SSDPSRV": "Manual",
    "upnphost": "Manual",
    "BITS": "Manual",
    "DoSvc": "AutomaticDelayed",
}

# Standard Windows protocols/providers bound to a physical adapter in a clean
# install. ms_implat (Multiplexor) is deliberately excluded: it is not bound
# by default.
ADAPTER_BINDINGS = ["ms_tcpip", "ms_tcpip6", "ms_pacer", "ms_msclient", "ms_server",
                    "ms_lltdio", "ms_rspndr", "ms_lldp"]


def clean_network_tweaks():
    """Delete gaming/ISO tweaks and restore Windows 11 factory values across
    Tcpip, AFD, Multimedia, policy keys and NCSI probing."""
    script = r'''
$cleaned = 0
$tcpKnown = @(
    'TcpAckFrequency','TCPNoDelay','TCPDelAckTicks','Tcp1323Opts','DefaultTTL',
    'GlobalMaxTcpWindowSize','TcpWindowSize','MaxUserPort','TcpTimedWaitDelay',
    'DisableTaskOffload','EnablePMTUDiscovery','EnablePMTUBHDetect','SackOpts',
    'EnableICMPRedirect','EnableDCA','EnableRSS','EnableTCPA','MaxFreeTcbs',
    'MaxHashTableSize','KeepAliveTime','SynAttackProtect','TCPMaxDataRetransmissions',
    'QualifyingDestinationThreshold'
)
$tcpRoots = @('HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters')
$tcpRoots += @(Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces' -ErrorAction SilentlyContinue | ForEach-Object { $_.PSPath })
foreach ($key in $tcpRoots) {
    foreach ($value in $tcpKnown) {
        if ($null -ne (Get-ItemProperty -Path $key -Name $value -ErrorAction SilentlyContinue)) {
            Remove-ItemProperty -Path $key -Name $value -ErrorAction SilentlyContinue
            $cleaned++
        }
    }
}
# NetBIOS over TCP/IP back to "use DHCP setting" (0) on every interface.
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces' -ErrorAction SilentlyContinue | ForEach-Object {
    $nb = (Get-ItemProperty -Path $_.PSPath -Name 'NetbiosOptions' -ErrorAction SilentlyContinue).NetbiosOptions
    if ($null -ne $nb -and $nb -ne 0) {
        Set-ItemProperty -Path $_.PSPath -Name 'NetbiosOptions' -Value 0 -Type DWord -ErrorAction SilentlyContinue
        $cleaned++
    }
}
# AFD driver: none of these values exist in a clean install.
$afdPath = 'HKLM:\SYSTEM\CurrentControlSet\Services\AFD\Parameters'
$afdKnown = @(
    'FastSendDatagramThreshold','FastCopyReceiveThreshold','DefaultReceiveWindow',
    'DefaultSendWindow','IgnorePushBitOnReceives','NonBlockingSendSpecialConsideration',
    'DisableRawSecurity','DynamicSendBufferDisable','EnableDynamicBacklog',
    'MinimumDynamicBacklog','MaximumDynamicBacklog','DynamicBacklogGrowthDelta'
)
if (Test-Path $afdPath) {
    foreach ($value in $afdKnown) {
        if ($null -ne (Get-ItemProperty -Path $afdPath -Name $value -ErrorAction SilentlyContinue)) {
            Remove-ItemProperty -Path $afdPath -Name $value -ErrorAction SilentlyContinue
            $cleaned++
        }
    }
}
# Multimedia profile: factory values are 10 and 20 (tweaks use 0xFFFFFFFF / 0).
$mmPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile'
if (Test-Path $mmPath) {
    Set-ItemProperty -Path $mmPath -Name 'NetworkThrottlingIndex' -Value 10 -Type DWord -ErrorAction SilentlyContinue
    Set-ItemProperty -Path $mmPath -Name 'SystemResponsiveness' -Value 20 -Type DWord -ErrorAction SilentlyContinue
    $cleaned += 2
}
# Policy keys written by ISOs/optimizers: deleting them restores defaults.
foreach ($policy in @(
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Psched',
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows\NetworkConnectivityStatusIndicator',
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization',
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows\TCPIP\v6Transition'
)) {
    if (Test-Path $policy) {
        Remove-Item -Path $policy -Recurse -Force -ErrorAction SilentlyContinue
        $cleaned++
    }
}
$dnsPolicy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\DNSClient'
if ($null -ne (Get-ItemProperty -Path $dnsPolicy -Name 'EnableMulticast' -ErrorAction SilentlyContinue)) {
    Remove-ItemProperty -Path $dnsPolicy -Name 'EnableMulticast' -ErrorAction SilentlyContinue
    $cleaned++
}
# NCSI active probing must be on or Windows shows "No internet" forever.
$ncsi = 'HKLM:\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet'
if (Test-Path $ncsi) {
    Set-ItemProperty -Path $ncsi -Name 'EnableActiveProbing' -Value 1 -Type DWord -ErrorAction SilentlyContinue
    $cleaned++
}
Write-Output "CleanedValues=$cleaned"
'''
    output = powershell(script, timeout=120)
    for line in output.splitlines():
        if line.startswith("CleanedValues="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                pass
    return 0


def restore_default_services(backup_path=None):
    """Restore the factory startup type of the essential network services and
    save the previous state to a JSON file first."""
    script = r'''
$names = @('BFE','Dhcp','Dnscache','NlaSvc','mpssvc','Wcmsvc','iphlpsvc','LanmanWorkstation','LanmanServer','WlanSvc','netprofm','WinHttpAutoProxySvc','SSDPSRV','upnphost','BITS','DoSvc')
$rows = @()
foreach ($n in $names) {
    $svc = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($null -ne $svc) {
        $rows += [pscustomobject]@{Name=$n; StartupType=[string]$svc.StartType; Status=[string]$svc.Status}
    }
}
ConvertTo-Json -InputObject $rows -Depth 3 -Compress
'''
    try:
        current = powershell_json(script, timeout=60)
        if backup_path:
            with open(backup_path, "w", encoding="utf-8") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=2)
    except Exception:
        # The state dump is only informational; never block the reset.
        pass

    script = r'''
$targets = @{
    'BFE'='Automatic'; 'Dhcp'='Automatic'; 'Dnscache'='Automatic'; 'NlaSvc'='Automatic';
    'mpssvc'='Automatic'; 'Wcmsvc'='Automatic'; 'iphlpsvc'='Automatic';
    'LanmanWorkstation'='Automatic'; 'LanmanServer'='Automatic'; 'WlanSvc'='Automatic';
    'netprofm'='Manual'; 'WinHttpAutoProxySvc'='Manual'; 'SSDPSRV'='Manual';
    'upnphost'='Manual'; 'BITS'='Manual'; 'DoSvc'='AutomaticDelayed'
}
$restored = 0
foreach ($name in $targets.Keys) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($null -eq $svc) { continue }
    $target = [string]$targets[$name]
    try {
        if ($target -eq 'AutomaticDelayed') {
            # sc.exe is the only reliable way to set "Automatic (Delayed Start)".
            sc.exe config $name start= delayed-auto | Out-Null
        } elseif ([string]$svc.StartType -ne $target) {
            Set-Service -Name $name -StartupType $target -ErrorAction Stop
        }
        if ($target -eq 'Automatic' -and $svc.Status -ne 'Running') {
            Start-Service -Name $name -ErrorAction SilentlyContinue
        }
        $restored++
    } catch {}
}
Write-Output "RestoredServices=$restored"
'''
    output = powershell(script, timeout=180)
    for line in output.splitlines():
        if line.startswith("RestoredServices="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                pass
    return 0


def restore_adapter_bindings(adapter_name):
    """Re-enable the standard Windows protocols bound to the adapter."""
    payload = base64.b64encode(adapter_name.encode("utf-8")).decode("ascii")
    script = r'''
$name = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))
$components = @('ms_tcpip','ms_tcpip6','ms_pacer','ms_msclient','ms_server','ms_lltdio','ms_rspndr','ms_lldp')
$enabled = 0
foreach ($comp in $components) {
    try {
        $binding = Get-NetAdapterBinding -Name $name -ComponentID $comp -ErrorAction SilentlyContinue
        if ($null -ne $binding -and -not $binding.Enabled) {
            Enable-NetAdapterBinding -Name $name -ComponentID $comp -ErrorAction Stop | Out-Null
            $enabled++
        }
    } catch {}
}
Write-Output "EnabledBindings=$enabled"
'''.replace("__PAYLOAD__", payload)
    output = powershell(script, timeout=60)
    for line in output.splitlines():
        if line.startswith("EnabledBindings="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                pass
    return 0


def reset_global_network(adapter_name=None):
    """Reset the whole Windows 11 network stack to factory values: services,
    adapter bindings, TCP globals, IPv6 transition, Winsock, TCP/IP, firewall,
    proxy, DNS/ARP caches and registry tweaks (Tcpip, AFD, Multimedia,
    policies, NCSI). Mandatory backups are created first; if one fails,
    nothing is modified."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, stamp)
    # netsh/reg exports fail if the target folder does not exist.
    os.makedirs(backup_dir, exist_ok=True)
    results = []

    # Mandatory backup 1: full firewall policy (rules and default actions).
    firewall_backup = os.path.join(backup_dir, "firewall.wfw")
    ok, detail = run_command(["netsh", "advfirewall", "export", firewall_backup], timeout=120)
    if not ok:
        return {"Aborted": True, "BackupPath": backup_dir, "Results": results, "Reason": detail}
    results.append({"Property": "Respaldo del firewall", "Success": True, "Detail": firewall_backup})

    # Mandatory backup 2: TCP/IP interface configuration (static IP/DNS).
    interfaces_backup = os.path.join(backup_dir, "tcpip_interfaces.reg")
    ok, detail = run_command(
        ["reg", "export", r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces", interfaces_backup, "/y"],
        timeout=60,
    )
    if not ok:
        return {"Aborted": True, "BackupPath": backup_dir, "Results": results, "Reason": detail}
    results.append({"Property": "Respaldo de interfaces TCP/IP", "Success": True, "Detail": interfaces_backup})

    # Optional backups: everything the deep cleanup below can touch.
    for key, filename in (
        (r"HKLM\SYSTEM\CurrentControlSet\Services\Winsock", "winsock.reg"),
        (r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters", "tcpip_parameters.reg"),
        (r"HKLM\SYSTEM\CurrentControlSet\Services\AFD\Parameters", "afd_parameters.reg"),
        (r"HKLM\SOFTWARE\Policies\Microsoft\Windows", "policies_windows.reg"),
        (r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile", "multimedia_systemprofile.reg"),
    ):
        ok, _ = run_command(["reg", "export", key, os.path.join(backup_dir, filename), "/y"], timeout=60)
        results.append({"Property": "Respaldo {}".format(filename), "Success": bool(ok),
                        "Detail": os.path.join(backup_dir, filename) if ok else "No crítico, se continúa"})

    # Local Group Policy file: backed up but never deleted automatically,
    # because it also contains non-network policies.
    pol_path = r"C:\Windows\System32\GroupPolicy\Machine\Registry.pol"
    if os.path.isfile(pol_path):
        try:
            shutil.copy2(pol_path, os.path.join(backup_dir, "Registry.pol.bak"))
            results.append({"Property": "Directivas locales (Registry.pol)", "Success": True,
                            "Detail": "Respaldo guardado; no se borra automáticamente"})
        except OSError:
            results.append({"Property": "Directivas locales (Registry.pol)", "Success": True, "Skipped": True,
                            "Detail": "No se pudo copiar el respaldo"})
    else:
        results.append({"Property": "Directivas locales (Registry.pol)", "Success": True, "Skipped": True,
                        "Detail": "No existe archivo de políticas locales"})

    # Optional backup: Wi-Fi profiles (Windows keeps them after the reset, so
    # this is only a precaution; XML files are exported without passwords).
    wlan_ok, _ = run_command(["netsh", "wlan", "export", "profile", "folder=" + backup_dir], timeout=60)
    results.append({"Property": "Respaldo de perfiles Wi-Fi", "Success": True,
                    "Skipped": not wlan_ok,
                    "Detail": os.path.join(backup_dir, "XML de perfiles (sin contraseñas)") if wlan_ok
                              else "Omitido: no hay Wi-Fi o el servicio no está activo"})

    joined = domain_joined()

    # Services first: BFE/Dhcp/mpssvc must be running for the resets below.
    try:
        svc_count = restore_default_services(os.path.join(backup_dir, "services_state.json"))
        results.append({"Property": "Servicios de red de Windows", "Success": True,
                        "Detail": "{} servicio(s) verificados/restaurados".format(svc_count)})
    except Exception as exc:
        results.append({"Property": "Servicios de red de Windows", "Success": False, "Detail": str(exc)})

    steps = [
        (["netsh", "winsock", "reset"], "Winsock: restablecido"),
        (["netsh", "int", "tcp", "reset"], "TCP: pila restablecida"),
        (["netsh", "int", "tcp", "set", "global", "autotuninglevel=normal"], "TCP Auto-Tuning: normal"),
        (["netsh", "int", "tcp", "set", "global", "rss=enabled"], "TCP RSS: habilitado"),
        (["netsh", "int", "tcp", "set", "global", "rsc=enabled"], "TCP RSC: habilitado"),
        (["netsh", "int", "tcp", "set", "global", "ecncapability=disabled"], "TCP ECN: deshabilitado (fábrica)"),
        (["netsh", "int", "tcp", "set", "global", "timestamps=disabled"], "TCP Timestamps: deshabilitado (fábrica)"),
        (["netsh", "int", "tcp", "set", "global", "initialrto=1000"], "TCP Initial RTO: 1000 (fábrica)"),
        (["netsh", "int", "tcp", "set", "global", "nonsackrttresiliency=disabled"], "TCP Non-SACK RTT: deshabilitado"),
        (["netsh", "int", "tcp", "set", "global", "maxsynretransmissions=4"], "TCP Max SYN Retransmissions: 4"),
        (["netsh", "int", "tcp", "set", "global", "fastopen=enabled"], "TCP Fast Open: habilitado"),
        (["netsh", "int", "tcp", "set", "global", "fastopenfallback=enabled"], "TCP Fast Open Fallback: habilitado"),
        (["netsh", "int", "tcp", "set", "global", "hystart=enabled"], "TCP HyStart: habilitado"),
        (["netsh", "int", "tcp", "set", "global", "pacingprofile=off"], "TCP Pacing: off"),
        (["netsh", "int", "tcp", "set", "supplemental", "template=internet", "congestionprovider=cubic"],
         "TCP Congestión: CUBIC (fábrica)"),
        (["netsh", "int", "ip", "reset"], "IPv4: pila y rutas restablecidas"),
        (["netsh", "int", "ipv6", "reset"], "IPv6: pila restablecida"),
        (["netsh", "interface", "teredo", "set", "state", "type=default"], "Teredo: estado por defecto"),
        (["netsh", "interface", "6to4", "set", "state", "state=default"], "6to4: estado por defecto"),
        (["netsh", "interface", "isatap", "set", "state", "state=default"], "ISATAP: estado por defecto"),
        (["netsh", "int", "udp", "reset"], "UDP global: restablecido"),
        (["netsh", "winhttp", "reset", "proxy"], "Proxy WinHTTP: restablecido"),
        (["arp", "-d", "*"], "Caché ARP: vaciada"),
        (["ipconfig", "/flushdns"], "Caché DNS: vaciada"),
    ]
    if not joined:
        steps.append((["netsh", "advfirewall", "reset"], "Firewall: restablecido a las reglas de fábrica"))
    for args, description in steps:
        ok, detail = run_command(args, timeout=120)
        if not ok:
            if description.startswith("UDP") or description.startswith("Caché ARP"):
                # These contexts are informational; never fail the whole reset.
                results.append({"Property": description, "Success": True, "Skipped": True,
                                "Detail": "Omitido en esta versión de Windows"})
            else:
                results.append({"Property": description, "Success": False, "Detail": detail})
        else:
            results.append({"Property": description, "Success": True, "Detail": "Restablecido"})

    # Deep registry cleanup (Tcpip, AFD, Multimedia, policies, NCSI).
    try:
        cleaned = clean_network_tweaks()
        results.append({"Property": "Registro profundo (AFD/TCP/QoS/Políticas)",
                        "Success": True,
                        "Detail": "{} valor(es)/clave(s) restaurados a fábrica".format(cleaned)})
    except Exception as exc:
        results.append({"Property": "Registro profundo", "Success": True, "Skipped": True,
                        "Detail": "Omitido: {}".format(exc)})

    # Re-enable the standard protocols bound to the adapter.
    if adapter_name:
        try:
            b_count = restore_adapter_bindings(adapter_name)
            results.append({"Property": "Protocolos enlazados al adaptador", "Success": True,
                            "Detail": "{} componente(s) reactivados".format(b_count)})
        except Exception as exc:
            results.append({"Property": "Protocolos enlazados al adaptador", "Success": False, "Detail": str(exc)})

    # Refresh local policies so reverted keys take effect.
    if not joined:
        ok, detail = run_command(["gpupdate", "/target:computer", "/force"], timeout=180)
        results.append({"Property": "Directivas locales (gpupdate)", "Success": ok,
                        "Detail": "Actualizadas" if ok else detail})

    return {"Aborted": False, "BackupPath": backup_dir, "Results": results, "DomainJoined": joined}


def latest_backup():
    try:
        if not os.path.isdir(BACKUP_ROOT):
            return None
        dirs = [os.path.join(BACKUP_ROOT, name) for name in os.listdir(BACKUP_ROOT)]
        dirs = [path for path in dirs
                if os.path.isfile(os.path.join(path, "firewall.wfw"))
                and os.path.isfile(os.path.join(path, "tcpip_interfaces.reg"))]
        return max(dirs, key=os.path.getmtime) if dirs else None
    except OSError:
        return None


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.lower().replace("&", " and ").split())


def valid_values(row):
    values = row.get("ValidDisplayValues") or []
    if isinstance(values, str):
        return [values]
    return [str(value) for value in values if value is not None and str(value).strip()]


def looks_wireless(adapter):
    text = normalize("{} {}".format(adapter.get("Description", ""), adapter.get("Name", "")))
    return any(word in text for word in ("wi-fi", "wifi", "wireless", "wlan"))


class NetworkDefaultsApp:
    BG = "#111318"
    PANEL = "#1a1d24"
    TEXT = "#f3f4f6"
    MUTED = "#a7afbc"

    def __init__(self, root):
        self.root = root
        self.root.title("Red · restaurar o dejar todo por defecto")
        self.root.geometry("860x620")
        self.root.minsize(760, 550)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.adapters = []
        self.properties = []
        self.adapter_by_label = {}
        self.store_path = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME, "baseline.json")

        self.build_ui()
        self.load_adapters()

    def build_ui(self):
        tk.Label(self.root, text="RED", bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=24, pady=(20, 1))
        tk.Label(self.root, text="Restaurar a como estaba antes de usarla, o dejar todo por defecto de Windows",
                 bg=self.BG, fg=self.MUTED, font=("Segoe UI", 10)).pack(anchor="w", padx=25, pady=(0, 14))

        top = tk.Frame(self.root, bg=self.PANEL, padx=14, pady=12)
        top.pack(fill="x", padx=20)
        tk.Label(top, text="Adaptador físico", bg=self.PANEL, fg=self.TEXT,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))
        self.adapter_var = tk.StringVar()
        self.adapter_combo = ttk.Combobox(top, textvariable=self.adapter_var, state="readonly", width=63)
        self.adapter_combo.pack(side="left", fill="x", expand=True)
        self.adapter_combo.bind("<<ComboboxSelected>>", self.on_adapter_selected)
        tk.Button(top, text="Actualizar", command=self.load_adapters, bg="#303744", fg=self.TEXT,
                  activebackground="#414b5b", activeforeground=self.TEXT, relief="flat", cursor="hand2").pack(side="left", padx=(10, 0), ipady=5, ipadx=8)

        self.status = tk.Label(self.root, text="Detectando adaptadores…", bg=self.BG, fg="#60a5fa",
                               font=("Segoe UI", 9, "bold"), anchor="w")
        self.status.pack(fill="x", padx=24, pady=(10, 7))

        table_frame = tk.Frame(self.root, bg=self.PANEL, padx=8, pady=8)
        table_frame.pack(fill="both", expand=True, padx=20)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Net.Treeview", background="#151820", foreground=self.TEXT, fieldbackground="#151820",
                        rowheight=25, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Net.Treeview.Heading", background="#242a34", foreground=self.TEXT,
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Net.Treeview", background=[("selected", "#21406b")], foreground=[("selected", "white")])
        self.table = ttk.Treeview(table_frame, columns=("property", "value", "options"), show="headings", style="Net.Treeview")
        self.table.heading("property", text="Propiedad del controlador")
        self.table.heading("value", text="Valor actual")
        self.table.heading("options", text="Opciones que admite")
        self.table.column("property", width=265, minwidth=180)
        self.table.column("value", width=180, minwidth=120)
        self.table.column("options", width=340, minwidth=190)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        buttons = tk.Frame(self.root, bg=self.BG)
        buttons.pack(fill="x", padx=20, pady=(14, 8))
        self.buttons = []
        for label, color, active, command in [
            ("Restaurar como estaba antes de usarla", "#334155", "#475569", self.restore_before),
            ("Dejar todo por defecto", "#0f766e", "#0d9488", self.reset_to_default),
        ]:
            button = tk.Button(buttons, text=label, command=command, bg=color, fg="white",
                               activebackground=active, activeforeground="white",
                               font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2")
            button.pack(side="left", fill="x", expand=True, padx=4, ipady=9)
            self.buttons.append(button)

        tk.Label(self.root,
                 text="Restaurar devuelve el adaptador (propiedades avanzadas, RSS y ahorro de energía) a como estaba la primera vez que se abrió la app, y si existe un respaldo del reset global permite recuperar también el firewall y la configuración IP (el adaptador se reinicia para releerla). Dejar todo por defecto restaura la red como en un Windows 11 recién instalado: adaptador a valores del controlador, protocolos enlazados (IPv4/IPv6, QoS, SMB, descubrimiento), servicios de red a su inicio de fábrica, Winsock, TCP/IP (IPv4 e IPv6), parámetros TCP globales, Teredo/6to4/ISATAP, proxy WinHTTP, caché DNS/ARP, firewall a reglas de fábrica, políticas de QoS/NCSI/Delivery Optimization y tweaks profundos de AFD/Tcpip/Multimedia. Crea respaldos obligatorios en %LOCALAPPDATA%\\RedCompetitiva\\ResetClean y requiere reiniciar Windows. En equipos de dominio el firewall no se toca. La tabla es solo de lectura.",
                 bg=self.BG, fg=self.MUTED, justify="left", wraplength=800,
                 font=("Segoe UI", 8)).pack(fill="x", padx=24, pady=(0, 14))

    def selected_adapter(self):
        return self.adapter_by_label.get(self.adapter_var.get())

    def load_adapters(self):
        try:
            self.adapters = list_adapters()
            self.adapter_by_label = {}
            labels = []
            for adapter in self.adapters:
                label = "{}  |  {}  |  {}  |  {}".format(
                    adapter.get("Name", "Adapter"), adapter.get("Description", ""),
                    adapter.get("Status", ""), adapter.get("LinkSpeed", ""),
                )
                labels.append(label)
                self.adapter_by_label[label] = adapter
            self.adapter_combo["values"] = labels
            if not labels:
                self.adapter_var.set("")
                self.status.configure(text="No se detectaron adaptadores físicos compatibles.", fg="#fbbf24")
                self.clear_table()
                return
            preferred = next((a for a in self.adapters if a.get("Status", "").lower() == "up"), self.adapters[0])
            preferred_label = next(label for label, adapter in self.adapter_by_label.items() if adapter is preferred)
            self.adapter_var.set(preferred_label)
            self.refresh_selected()
        except Exception as exc:
            self.status.configure(text="No se pudieron leer los adaptadores.", fg="#f87171")
            messagebox.showerror("Error de detección", str(exc), parent=self.root)

    def clear_table(self):
        for row in self.table.get_children():
            self.table.delete(row)

    def on_adapter_selected(self, _event=None):
        self.refresh_selected()

    def refresh_selected(self):
        adapter = self.selected_adapter()
        if not adapter:
            return
        try:
            self.properties = get_properties(adapter["Name"])
            self.clear_table()
            for prop in self.properties:
                display_name = prop.get("DisplayName")
                if not display_name:
                    continue
                valid = valid_values(prop)
                if not valid:
                    minimum = prop.get("NumericParameterMinValue")
                    maximum = prop.get("NumericParameterMaxValue")
                    step = prop.get("NumericParameterStepValue")
                    if minimum not in (None, "") and maximum not in (None, ""):
                        valid = ["{}–{} (paso {})".format(minimum, maximum, step or "1")]
                self.table.insert("", "end", values=(
                    display_name,
                    prop.get("DisplayValue", ""),
                    ", ".join(valid) if valid else "—",
                ))
            self.ensure_baseline(adapter, self.properties)
            self.status.configure(
                text="{} · enlace {} · {} propiedades disponibles".format(
                    adapter.get("Description", adapter.get("Name", "NIC")),
                    adapter.get("LinkSpeed", "velocidad desconocida"),
                    len(self.properties),
                ), fg="#4ade80" if adapter.get("Status", "").lower() == "up" else "#fbbf24")
        except Exception as exc:
            self.status.configure(text="No se pudieron leer las propiedades del controlador.", fg="#f87171")
            self.clear_table()
            messagebox.showerror("Error del adaptador", str(exc), parent=self.root)

    def load_store(self):
        try:
            with open(self.store_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

    def write_store(self, data):
        folder = os.path.dirname(self.store_path)
        os.makedirs(folder, exist_ok=True)
        temp_path = self.store_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.store_path)

    @staticmethod
    def adapter_key(adapter):
        return adapter.get("Guid") or adapter.get("Description") or adapter.get("Name")

    def ensure_baseline(self, adapter, properties):
        """First time an adapter is seen, snapshot its full state: that is the
        'how it was before using this app' reference used by the restore."""
        store = self.load_store()
        key = self.adapter_key(adapter)
        if key not in store:
            store[key] = {
                "name": adapter.get("Name"),
                "description": adapter.get("Description"),
                "rssEnabled": get_rss_state(adapter.get("Name")),
                "properties": [
                    {"RegistryKeyword": prop.get("RegistryKeyword"), "RegistryValue": prop.get("RegistryValue"),
                     "Label": prop.get("DisplayName") or prop.get("RegistryKeyword")}
                    for prop in properties if prop.get("RegistryKeyword") and prop.get("RegistryValue") is not None
                ],
                "powerManagement": get_power_management_state(adapter.get("Name")),
            }
            self.write_store(store)
            return
        if "powerManagement" not in store[key]:
            store[key]["powerManagement"] = get_power_management_state(adapter.get("Name"))
            self.write_store(store)

    def restore_changes(self, adapter):
        """Build the per-property restore list from the saved baseline."""
        entry = self.load_store().get(self.adapter_key(adapter))
        if not entry:
            return [], None, None
        current = {prop.get("RegistryKeyword"): prop.get("RegistryValue") for prop in self.properties}

        def signature(value):
            if isinstance(value, list):
                return tuple(str(item) for item in value)
            return (str(value),) if value is not None else ()

        changes = []
        for prop in entry.get("properties", []):
            keyword = prop.get("RegistryKeyword")
            # Saved baselines can contain stale or non-NIC keys (for example
            # ASPM from a power plan). Restore only keywords exposed by this
            # exact adapter and driver right now.
            if (not keyword or keyword not in current or prop.get("RegistryValue") is None
                    or signature(current.get(keyword)) == signature(prop.get("RegistryValue"))):
                continue
            changes.append({
                "RegistryKeyword": keyword,
                "RegistryValue": prop.get("RegistryValue"),
                "Label": prop.get("Label") or keyword,
                "Restore": True,
            })

        rss_target = entry.get("rssEnabled")
        if not isinstance(rss_target, bool):
            rss_target = None

        saved = entry.get("powerManagement")
        power_target = None
        if isinstance(saved, dict):
            state = {
                field: value for field, value in saved.items()
                if field in ("SelectiveSuspend", "DeviceSleepOnDisconnect", "D0PacketCoalescing")
                and value in ("Enabled", "Disabled")
            }
            if state:
                power_target = {"Mode": "restore", "State": state}
        return changes, rss_target, power_target

    def report_results(self, results):
        failed = [row for row in results if not row.get("Success")]
        ok = sum(1 for row in results if row.get("Success") and not row.get("Skipped"))
        if failed:
            summary = "\n".join("• {}: {}".format(row.get("Property"), row.get("Detail")) for row in failed[:8])
            self.status.configure(text="Aplicado parcialmente · {} correcto(s), {} error(es)".format(ok, len(failed)), fg="#fbbf24")
            messagebox.showwarning("Aplicación parcial", summary, parent=self.root)
            return False
        if ok == 0:
            self.status.configure(text="Sin cambios necesarios: ya estaba en ese estado.", fg="#fbbf24")
        else:
            self.status.configure(text="Aplicado · {} acciones correctas".format(ok), fg="#4ade80")
        return True

    def restore_before(self):
        """Restore the adapter to the state saved the first time the app saw
        it, plus (optionally) firewall/IP config from the last global backup."""
        adapter = self.selected_adapter()
        if not adapter:
            messagebox.showwarning("Sin adaptador", "Seleccioná un adaptador físico primero.", parent=self.root)
            return
        try:
            self.properties = get_properties(adapter["Name"])
            self.ensure_baseline(adapter, self.properties)
            changes, rss_target, power_target = self.restore_changes(adapter)
            current_rss = get_rss_state(adapter["Name"])
            if current_rss is None or current_rss == rss_target:
                rss_target = None
            if power_target is not None:
                current_pm = get_power_management_state(adapter["Name"])
                if current_pm is None:
                    power_target = None
                else:
                    state = power_target.get("State", {})
                    if not any(current_pm.get(field) != value for field, value in state.items()):
                        power_target = None
        except Exception as exc:
            messagebox.showerror("No se pudo preparar la restauración", str(exc), parent=self.root)
            return
        if not changes and rss_target is None and power_target is None:
            messagebox.showinfo("Sin cambios necesarios", "El adaptador ya está exactamente como estaba antes de usar la app.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Restaurar como estaba antes",
            "Se restaurarán {} ajuste(s) del adaptador a como estaba antes de usar la app. Internet se cortará unos segundos. No hace falta reiniciar Windows. ¿Continuar?".format(len(changes) + (1 if rss_target is not None else 0) + (1 if power_target is not None else 0)),
            parent=self.root,
        ):
            return
        backup = latest_backup()
        restore_global = False
        if backup:
            restore_global = messagebox.askyesno(
                "Restaurar también el reset global",
                "Existe un respaldo del último reset global:\n{}\n\n¿Restaurar también el firewall y la configuración IP desde ese respaldo? El adaptador se reiniciará para releerla.".format(backup),
                parent=self.root,
            )
        adapter_name = adapter["Name"]
        for button in self.buttons:
            button.configure(state="disabled")
        self.status.configure(text="Restaurando a como estaba antes…", fg="#fbbf24")
        self.root.update_idletasks()

        def worker():
            # Blocking subprocess calls run off the Tk thread.
            results = []
            error = None
            try:
                results = list(set_properties(
                    adapter_name, changes,
                    rss_enabled=rss_target,
                    power_management=power_target,
                    reset_advanced=False,
                    restart=True,
                ))
                if restore_global and backup:
                    fw_ok, fw_detail = run_command(["netsh", "advfirewall", "import", os.path.join(backup, "firewall.wfw")], timeout=120)
                    results.append({"Property": "Firewall", "Success": fw_ok,
                                    "Detail": "Restaurado desde el respaldo" if fw_ok else fw_detail})
                    ip_ok, ip_detail = run_command(["reg", "import", os.path.join(backup, "tcpip_interfaces.reg")], timeout=60)
                    results.append({"Property": "Configuración IP", "Success": ip_ok,
                                    "Detail": "Restaurada desde el respaldo" if ip_ok else ip_detail})
                    if ip_ok:
                        # Force Windows to re-read the interface parameters
                        # instead of requiring a full reboot.
                        try:
                            restart_adapter(adapter_name)
                            results.append({"Property": "Adaptador (recarga IP)", "Success": True,
                                            "Detail": "Reiniciado para releer la configuración"})
                        except Exception as exc:
                            results.append({"Property": "Adaptador (recarga IP)", "Success": False,
                                            "Detail": str(exc)})
            except Exception as exc:
                error = str(exc)
            self.root.after(0, lambda: self.finish_restore(results, error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_restore(self, results, error):
        for button in self.buttons:
            button.configure(state="normal")
        if error:
            self.status.configure(text="Error al restaurar.", fg="#f87171")
            messagebox.showerror("Error de configuración", error, parent=self.root)
            return
        self.report_results(results)
        try:
            self.refresh_selected()
        except Exception:
            pass

    def reset_to_default(self):
        """Everything back to Windows defaults: adapter to driver INF values,
        RSS on, adapter power saving on, Winsock/TCP/IP/TCP/UDP, WinHTTP
        proxy, DNS cache, firewall factory rules and known tweaks removed."""
        adapter = self.selected_adapter()
        if not adapter:
            messagebox.showwarning("Sin adaptador", "Seleccioná un adaptador físico primero.", parent=self.root)
            return
        wifi_note = ("\n\nATENCIÓN: este adaptador parece Wi-Fi. Resetear sus propiedades avanzadas puede "
                     "borrar el país/canal configurado: revisá después que siga viendo tus redes de 5/6 GHz.") if looks_wireless(adapter) else ""
        if not messagebox.askyesno(
            "Dejar todo por defecto",
            "RESET COMPLETO A FÁBRICA. Antes se guardarán respaldos en %LOCALAPPDATA%\\RedCompetitiva\\ResetClean. "
            "Se restablecerán: propiedades del adaptador, protocolos enlazados (IPv4/IPv6, QoS, SMB, descubrimiento), servicios de red (DHCP, DNS, firewall, WLAN, SMB...), Winsock, TCP/IP (IPv4 e IPv6), parámetros TCP globales, Teredo/6to4/ISATAP, proxy WinHTTP, caché DNS/ARP, firewall, políticas de QoS/NCSI/Delivery Optimization y tweaks profundos (AFD, Tcpip, Multimedia). "
            "El firewall perderá sus reglas personalizadas. TCP/IP puede borrar IP/DNS estáticos; VPN/proxy y reglas propias podrían necesitar reconfiguración. Se reactivarán servicios y protocolos que alguna ISO pudo haber deshabilitado (incluido el uso compartido SMB). En equipos de dominio el firewall y las políticas administradas se conservan. "
            "Se requiere reiniciar Windows. ¿Continuar?" + wifi_note,
            parent=self.root,
        ):
            return
        adapter_name = adapter["Name"]
        for button in self.buttons:
            button.configure(state="disabled")
        self.status.configure(text="Creando respaldos y restableciendo todo a los valores de Windows…", fg="#fbbf24")
        self.root.update_idletasks()

        def worker():
            info = None
            error = None
            try:
                info = reset_global_network(adapter_name=adapter_name)
                if not info.get("Aborted"):
                    try:
                        info["Results"] = list(info.get("Results", [])) + list(set_properties(
                            adapter_name, [],
                            rss_enabled=True,
                            power_management={"Mode": "enable"},
                            reset_advanced=True,
                            restart=True,
                        ))
                    except Exception as exc:
                        info["Results"] = list(info.get("Results", [])) + [
                            {"Property": "Adaptador", "Success": False, "Detail": str(exc)}]
            except Exception as exc:
                error = str(exc)
            self.root.after(0, lambda: self.finish_reset(info, error))

        threading.Thread(target=worker, daemon=True).start()

    def finish_reset(self, info, error):
        for button in self.buttons:
            button.configure(state="normal")
        if error:
            self.status.configure(text="Error en el reset.", fg="#f87171")
            messagebox.showerror("Error de configuración", error, parent=self.root)
            return
        if info.get("Aborted"):
            backup_path = info.get("BackupPath", "(sin ruta)")
            self.status.configure(text="Reset cancelado: no se pudo crear el respaldo imprescindible.", fg="#f87171")
            messagebox.showerror(
                "Reset cancelado",
                "No se modificó la red porque falló el respaldo de la configuración de interfaces o del firewall.\n\n"
                "Detalle: {}\n\nArchivos generados (si los hubo):\n{}".format(info.get("Reason", ""), backup_path),
                parent=self.root,
            )
            return
        domain_note = "\n\nLas políticas de dominio se conservaron." if info.get("DomainJoined") else ""
        if self.report_results(info.get("Results", [])):
            self.status.configure(text="Todo por defecto · reinicio requerido · respaldo guardado", fg="#4ade80")
            messagebox.showinfo(
                "Reset completo aplicado",
                "Todo quedó en los valores por defecto de Windows y falta reiniciar Windows para terminar.\n\n"
                "Respaldo: {}{}\n\n"
                "Después del reinicio, si algo quedó mal, el botón Restaurar puede recuperar firewall e IP desde este respaldo.".format(
                    info.get("BackupPath"), domain_note),
                parent=self.root,
            )
        try:
            self.refresh_selected()
        except Exception:
            pass


def main():
    if os.name != "nt":
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Windows requerido", "Esta aplicación funciona únicamente en Windows 10/11.")
        return
    if not is_admin():
        # UAC launches can start in System32 and argv[0] may be relative.
        script_path = os.path.abspath(sys.argv[0])
        params = subprocess.list2cmdline([script_path] + sys.argv[1:])
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
        if result <= 32:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Permiso requerido", "Ejecutá la aplicación como administrador para cambiar el controlador de red.")
        return
    root = tk.Tk()
    NetworkDefaultsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
