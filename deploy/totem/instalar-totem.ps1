<#
.SYNOPSIS
  Deja un tótem Windows listo como cajero de autoservicio CESSA. Se corre UNA vez, como administrador.

.DESCRIPTION
  - Crea el usuario local del kiosco (sin permisos de administrador) con una clave al azar que
    nadie necesita conocer, y lo deja con inicio de sesión automático (la clave queda en el
    almacén protegido de Windows, como hace Autologon de Sysinternals, no en el registro).
  - Copia iniciar-kiosco.ps1 a C:\Kiosco y crea la tarea "Kiosco CESSA" que lo abre al iniciar sesión.
  - Pide el token de la pantalla (no se muestra) y lo guarda en C:\Kiosco\config.json, que solo
    pueden leer los administradores y el usuario del kiosco.
  - Revisa que test01.cessa.com.bo apunte a la IP interna de la .88 (si no, lo fija en hosts). El
    certificado es de Let's Encrypt: no hay que instalar nada.
  - Pantalla siempre encendida, sin suspensión, sin pantalla de bloqueo, sin gestos de borde,
    actualizaciones fuera del horario de atención y reinicio diario de madrugada.
  - Políticas de Chrome para que no muestre traductor, guardar contraseñas ni avisos.

  El driver de la impresora (ZKP8008) se instala aparte, antes o después; después se indica su
  nombre con -Impresora (o se vuelve a correr este script).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\instalar-totem.ps1 -Impresora "ZKP8008"

.EXAMPLE
  # Test en vez de prod (QR simulado):
  .\instalar-totem.ps1 -Url https://test01.cessa.com.bo/kiosco/

.EXAMPLE
  # Sacar todo (vuelve a ser un Windows normal; el usuario del kiosco se borra):
  .\instalar-totem.ps1 -Desinstalar
#>
[CmdletBinding()]
param(
    [string]$Url = "https://test01.cessa.com.bo:8443/kiosco/",
    [string]$IpServidor = "10.1.1.88",
    [string]$Impresora = "",
    [ValidateSet("termica80", "termica58", "carta")][string]$Formato = "termica80",
    [string]$Usuario = "kiosco",
    [string]$ReinicioDiario = "03:30",
    [string]$HorarioActivo = "06-22",
    [switch]$SinToken,
    [switch]$Desinstalar
)
$ErrorActionPreference = "Stop"
$Base = "C:\Kiosco"
$Tarea = "Kiosco CESSA"
$TareaReinicio = "Kiosco CESSA - reinicio diario"
$Winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Hay que correrlo como administrador (clic derecho > Ejecutar como administrador)." }

function Paso($texto) { Write-Host "`n== $texto" -ForegroundColor Cyan }
function Ok($texto) { Write-Host "   $texto" -ForegroundColor Green }
function Aviso($texto) { Write-Host "   $texto" -ForegroundColor Yellow }

# --- Clave del inicio automático en el almacén LSA (igual que Autologon de Sysinternals) ---------
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class LsaSecreto {
    [StructLayout(LayoutKind.Sequential)] struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)] struct LSA_OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName; public uint Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
    [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr SystemName, ref LSA_OBJECT_ATTRIBUTES ObjectAttributes, uint DesiredAccess, out IntPtr PolicyHandle);
    [DllImport("advapi32.dll")] static extern uint LsaStorePrivateData(IntPtr PolicyHandle, ref LSA_UNICODE_STRING KeyName, IntPtr PrivateData);
    [DllImport("advapi32.dll")] static extern uint LsaStorePrivateData(IntPtr PolicyHandle, ref LSA_UNICODE_STRING KeyName, ref LSA_UNICODE_STRING PrivateData);
    [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr PolicyHandle);
    [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint Status);
    static LSA_UNICODE_STRING Texto(string s) {
        var u = new LSA_UNICODE_STRING();
        u.Buffer = Marshal.StringToHGlobalUni(s);
        u.Length = (ushort)(s.Length * 2);
        u.MaximumLength = (ushort)(s.Length * 2 + 2);
        return u;
    }
    public static void Guardar(string clave, string valor) {
        var atributos = new LSA_OBJECT_ATTRIBUTES();
        IntPtr politica;
        uint r = LsaOpenPolicy(IntPtr.Zero, ref atributos, 0x00000020 | 0x00000004, out politica); // CREATE_SECRET | GET_PRIVATE_INFORMATION
        if (r != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(r));
        var nombre = Texto(clave);
        try {
            if (valor == null) { r = LsaStorePrivateData(politica, ref nombre, IntPtr.Zero); }
            else { var dato = Texto(valor); try { r = LsaStorePrivateData(politica, ref nombre, ref dato); } finally { Marshal.ZeroFreeGlobalAllocUnicode(dato.Buffer); } }
            if (r != 0 && valor != null) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(r));
        } finally { Marshal.FreeHGlobal(nombre.Buffer); LsaClose(politica); }
    }
}
"@

# ==================================================================================================
if ($Desinstalar) {
    Paso "Quitando el kiosco"
    foreach ($t in $Tarea, $TareaReinicio) { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }
    Set-ItemProperty $Winlogon -Name AutoAdminLogon -Value "0"
    Remove-ItemProperty $Winlogon -Name DefaultUserName, DefaultPassword -ErrorAction SilentlyContinue
    [LsaSecreto]::Guardar("DefaultPassword", $null)
    Ok "Sin inicio automático ni tareas."
    if (Get-LocalUser -Name $Usuario -ErrorAction SilentlyContinue) {
        $resp = Read-Host "   ¿Borrar también el usuario '$Usuario' y su perfil? (s/n)"
        if ($resp -eq "s") {
            $sid = (Get-LocalUser $Usuario).SID.Value
            Remove-LocalUser -Name $Usuario
            Get-CimInstance Win32_UserProfile | Where-Object SID -eq $sid | Remove-CimInstance -ErrorAction SilentlyContinue
            Ok "Usuario borrado."
        }
    }
    Remove-Item "HKLM:\SOFTWARE\Policies\Google\Chrome" -Recurse -ErrorAction SilentlyContinue
    Aviso "C:\Kiosco se deja (tiene los registros). Bórrala a mano si ya no sirve."
    $hosts = "$env:SystemRoot\System32\drivers\etc\hosts"
    Set-Content $hosts @(Get-Content $hosts -Encoding ASCII | Where-Object { $_ -notmatch "# kiosco CESSA" }) -Encoding ASCII
    Aviso "Energía y pantalla de bloqueo quedan como estaban."
    exit 0
}

# ==================================================================================================
Paso "Archivos en $Base"
$origen = Join-Path $PSScriptRoot "iniciar-kiosco.ps1"
if (-not (Test-Path $origen)) { throw "Falta iniciar-kiosco.ps1 junto a este script." }
New-Item -ItemType Directory -Force -Path $Base, (Join-Path $Base "logs") | Out-Null
Copy-Item $origen (Join-Path $Base "iniciar-kiosco.ps1") -Force
Ok "iniciar-kiosco.ps1 copiado."

# --------------------------------------------------------------------------------------------------
Paso "Usuario del kiosco '$Usuario'"
$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$clave = [Convert]::ToBase64String($bytes) + "a1!"
$segura = ConvertTo-SecureString $clave -AsPlainText -Force
if (Get-LocalUser -Name $Usuario -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name $Usuario -Password $segura -PasswordNeverExpires $true -UserMayChangePassword $false
    Enable-LocalUser -Name $Usuario
    Ok "Ya existía: se le puso una clave nueva al azar."
} else {
    New-LocalUser -Name $Usuario -Password $segura -FullName "Cajero CESSA" -Description "Cajero de autoservicio (inicio automático)" `
        -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    Ok "Creado."
}
$grupoUsuarios = (New-Object Security.Principal.SecurityIdentifier "S-1-5-32-545").Translate([Security.Principal.NTAccount]).Value.Split("\")[-1]
Add-LocalGroupMember -Group $grupoUsuarios -Member $Usuario -ErrorAction SilentlyContinue
$grupoAdmin = (New-Object Security.Principal.SecurityIdentifier "S-1-5-32-544").Translate([Security.Principal.NTAccount]).Value.Split("\")[-1]
if (Get-LocalGroupMember -Group $grupoAdmin -Member $Usuario -ErrorAction SilentlyContinue) {
    Remove-LocalGroupMember -Group $grupoAdmin -Member $Usuario
    Aviso "Era administrador: se le quitó."
}

Set-ItemProperty $Winlogon -Name AutoAdminLogon -Value "1"
Set-ItemProperty $Winlogon -Name DefaultUserName -Value $Usuario
Set-ItemProperty $Winlogon -Name DefaultDomainName -Value $env:COMPUTERNAME
Remove-ItemProperty $Winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
Remove-ItemProperty $Winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
[LsaSecreto]::Guardar("DefaultPassword", $clave)
$clave = $null; $segura = $null
Ok "Inicio de sesión automático activado (para entrar como otro usuario: mantener Shift al arrancar)."

# --------------------------------------------------------------------------------------------------
Paso "Configuración de la pantalla"
$archivoCfg = Join-Path $Base "config.json"
$anterior = $null
if (Test-Path $archivoCfg) { $anterior = Get-Content $archivoCfg -Raw -Encoding UTF8 | ConvertFrom-Json }
$token = ""
if (-not $SinToken) {
    $leido = Read-Host "   Token de la pantalla (KIOSCO_TOKENS del servidor; Enter = dejar el que está)" -AsSecureString
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($leido))
}
if (-not $token -and $anterior) { $token = $anterior.token }
if (-not $token) { throw "Hace falta el token de la pantalla." }
if (-not $Impresora -and $anterior) { $Impresora = $anterior.impresora }
[ordered]@{ url = $Url; token = $token; impresion = $Formato; impresora = $Impresora } |
    ConvertTo-Json | Set-Content $archivoCfg -Encoding UTF8
$token = $null
# Solo administradores, SYSTEM y el usuario del kiosco pueden leer config.json.
$acl = New-Object Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($quien in "*S-1-5-32-544", "*S-1-5-18") {
    $sid = New-Object Security.Principal.SecurityIdentifier ($quien.TrimStart("*"))
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, "FullControl", "Allow")))
}
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule("$env:COMPUTERNAME\$Usuario", "Read", "Allow")))
Set-Acl $archivoCfg $acl
# El kiosco escribe sus registros en C:\Kiosco\logs.
$aclLogs = Get-Acl (Join-Path $Base "logs")
$aclLogs.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule("$env:COMPUTERNAME\$Usuario", "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")))
Set-Acl (Join-Path $Base "logs") $aclLogs
Ok "Servidor: $Url  ·  formato: $Formato  ·  impresora: $(if ($Impresora) { $Impresora } else { '(la predeterminada)' })"

if ($Impresora -and -not (Get-Printer -Name $Impresora -ErrorAction SilentlyContinue)) {
    Aviso "No está instalada la impresora '$Impresora'. Instala el driver y revisa el nombre con Get-Printer."
    Get-Printer | ForEach-Object { Aviso "   instalada: $($_.Name)" }
}

# --------------------------------------------------------------------------------------------------
Paso "Nombre del servidor"
# El certificado de la .88 es de Let's Encrypt para test01.cessa.com.bo (Windows ya confía en él):
# hay que entrar por ese nombre, no por la IP (por IP Chrome siempre avisa). El nombre tiene que
# resolver a la IP interna: si resolviera a la pública, el pedido sale a internet y el servidor lo
# rechaza (403, fuera de KIOSCO_REDES).
$servidor = ([uri]$Url).Host
$hosts = "$env:SystemRoot\System32\drivers\etc\hosts"
$marca = "# kiosco CESSA"
$lineas = @(Get-Content $hosts -Encoding ASCII | Where-Object { $_ -notmatch [regex]::Escape($marca) })
Set-Content $hosts $lineas -Encoding ASCII
Clear-DnsClientCache
$resuelto = $null
try { $resuelto = (Resolve-DnsName $servidor -Type A -ErrorAction Stop | Where-Object IPAddress | Select-Object -First 1).IPAddress } catch {}
if ($resuelto -eq $IpServidor) {
    Ok "$servidor -> $resuelto (DNS interno)."
} elseif ($servidor -match '^\d+\.\d+\.\d+\.\d+$') {
    Aviso "La URL usa la IP: Chrome va a mostrar el aviso de certificado. Usa https://test01.cessa.com.bo:8443/kiosco/."
} else {
    Add-Content $hosts "$IpServidor`t$servidor`t$marca" -Encoding ASCII
    Clear-DnsClientCache
    Aviso "$servidor resolvía a '$resuelto': se fijó a $IpServidor en el archivo hosts."
}

# --------------------------------------------------------------------------------------------------
Paso "Energía, bloqueo y gestos"
powercfg /change monitor-timeout-ac 0 | Out-Null
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change disk-timeout-ac 0 | Out-Null
powercfg /hibernate off | Out-Null
function Politica($ruta, $nombre, $valor) {
    New-Item -Path $ruta -Force | Out-Null
    Set-ItemProperty -Path $ruta -Name $nombre -Value $valor -Type DWord
}
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" NoLockScreen 1
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\EdgeUI" AllowEdgeSwipe 0
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" DisableWindowsConsumerFeatures 1
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" DisableSoftLanding 1
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Explorer" DisableNotificationCenter 1
Ok "Pantalla siempre encendida, sin suspensión, sin pantalla de bloqueo ni gestos de borde."

$inicio, $fin = $HorarioActivo.Split("-") | ForEach-Object { [int]$_ }
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" SetActiveHours 1
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" ActiveHoursStart $inicio
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" ActiveHoursEnd $fin
Politica "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" NoAutoRebootWithLoggedOnUsers 1
Ok "Windows Update no reinicia entre las $inicio y las $fin."

# --------------------------------------------------------------------------------------------------
Paso "Políticas de Chrome"
$pc = "HKLM:\SOFTWARE\Policies\Google\Chrome"
New-Item -Path $pc -Force | Out-Null
$politicasChrome = @{
    TranslateEnabled = 0; PasswordManagerEnabled = 0; AutofillAddressEnabled = 0; AutofillCreditCardEnabled = 0
    BrowserSignin = 0; SyncDisabled = 1; PromotionsEnabled = 0; HideFirstRunExperience = 1
    DefaultBrowserSettingEnabled = 0; MetricsReportingEnabled = 0; BackgroundModeEnabled = 0
    DefaultNotificationsSetting = 2; DefaultGeolocationSetting = 2; PrintPreviewUseSystemDefaultPrinter = 1
    BookmarkBarEnabled = 0; DeveloperToolsAvailability = 2; IncognitoModeAvailability = 1
}
foreach ($p in $politicasChrome.GetEnumerator()) { Set-ItemProperty $pc -Name $p.Key -Value $p.Value -Type DWord }
Ok "Sin traductor, contraseñas, notificaciones ni herramientas de desarrollo."
$chromeOk = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ }
if (-not $chromeOk) { Aviso "Chrome no está instalado para todos los usuarios: instálalo (versión empresarial MSI)." }

# --------------------------------------------------------------------------------------------------
Paso "Tareas programadas"
$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Base\iniciar-kiosco.ps1`""
$disparo = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$Usuario"
$quien = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$Usuario" -LogonType Interactive -RunLevel Limited
$ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Tarea -Action $accion -Trigger $disparo -Principal $quien -Settings $ajustes -Force | Out-Null
Ok "'$Tarea': abre el kiosco al iniciar sesión '$Usuario'."

if ($ReinicioDiario) {
    $accionR = New-ScheduledTaskAction -Execute "shutdown.exe" -Argument "/r /f /t 60 /c `"Reinicio diario del cajero`""
    $disparoR = New-ScheduledTaskTrigger -Daily -At $ReinicioDiario
    $sistema = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TareaReinicio -Action $accionR -Trigger $disparoR -Principal $sistema -Force | Out-Null
    Ok "'$TareaReinicio': todos los días a las $ReinicioDiario."
}

# --------------------------------------------------------------------------------------------------
Paso "Prueba de conexión desde este equipo"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10
    Ok "El servidor responde ($($r.StatusCode))."
} catch {
    $codigo = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { $_.Exception.Message }
    Aviso "No responde bien: $codigo"
    if ("$codigo" -eq "403") { Aviso "403 = la IP de este equipo no está en KIOSCO_REDES del servidor." }
}
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -ne "WellKnown" -and $_.IPAddress -notlike "169.254*" } | Select-Object -First 1).IPAddress
Write-Host "   IP de este equipo: $ip (tiene que estar dentro de KIOSCO_REDES)"

Write-Host "`nListo. Al reiniciar, el tótem entra solo como '$Usuario', espera al servidor, abre el cajero" -ForegroundColor Green
Write-Host "y la pantalla hace la revisión de arranque. Registros: $Base\logs" -ForegroundColor Green
Write-Host "Mantenimiento: crear el archivo $Base\detener (no reabre Chrome) o mantener Shift al encender." -ForegroundColor Green
$resp = Read-Host "`n¿Reiniciar ahora? (s/n)"
if ($resp -eq "s") { Restart-Computer -Force }
