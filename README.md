# Red Competitiva

Utilidad de Windows para optimizar y restablecer la configuración de red (TCP/IP, firewall, servicios, DNS y perfiles inalámbricos), pensada para entornos competitivos.

## Descarga rápida (no necesitas instalar Python)

| Archivo | Para quién |
|---|---|
| [`releases/Red_Competitiva_x64.exe`](releases/Red_Competitiva_x64.exe) | Windows **10/11 de 64 bits** (la mayoría de los PCs) |
| [`releases/Red_Competitiva_x86.exe`](releases/Red_Competitiva_x86.exe) | Windows **10/11 de 32 bits** (PCs antiguos) |

**Cómo elegir:** *Configuración → Sistema → Acerca de → Tipo de sistema*. Si dice «Sistema operativo de 64 bits», usa `_x64.exe`.

- Descarga el `.exe` y haz doble clic. **No requiere Python ni dependencias**.
- Aparecerá la ventana de **UAC**: pulsa «Sí». La app necesita administrador para modificar red, firewall y servicios.
- Antes de aplicar cambios, la herramienta crea una copia de seguridad en `%LOCALAPPDATA%\Red Competitiva\ResetClean`, con el firewall, el registro TCP/IP, los servicios y los perfiles Wi-Fi.
- Algunos antivirus pueden alertar por un ejecutable sin firma digital (compilado con PyInstaller). Es un falso positivo conocido; el código fuente está disponible para revisarlo.

## Ejecutar desde el código fuente

```powershell
python Red_Competitiva.pyw
```

Generar el ejecutable:

```powershell
pyinstaller --noconfirm --onefile --windowed Red_Competitiva.pyw
```

## Requisitos

- Windows 10 u 11 (la propia aplicación lo comprueba al iniciar).
- Permisos de administrador (elevación UAC automática).
- Para el `.exe`: ningún requisito adicional. Para el `.pyw`: Python 3.x.

## Compatibilidad analizada

| Aspecto | Resultado |
|---|---|
| Dependencias externas | Ninguna: solo biblioteca estándar de Python |
| Python embebido | Sí, incluido dentro del `.exe` |
| Arquitecturas | x64 y x86 (no ARM64) |
| Windows 7 / 8.1 | No compatibles con Python 3.15 |
| Comandos usados | `netsh`, `reg`, `sc`, `powershell`, `powercfg` (incluidos en Windows) |
| Copias de seguridad | Sí, en `%LOCALAPPDATA%` antes de aplicar cambios |
| Elevación de permisos | Automática vía UAC |

## Aviso

Esta herramienta modifica ajustes de red del sistema. Revísala antes de usarla y conserva siempre la copia de seguridad de tus perfiles. En equipos de dominio o gestionados por una organización, las políticas corporativas pueden conservarse o bloquear algunos cambios.
