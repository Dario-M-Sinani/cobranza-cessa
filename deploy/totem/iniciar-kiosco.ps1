<#
  Arranque del cajero de autoservicio en el tótem. Lo ejecuta la tarea programada "Kiosco CESSA"
  cada vez que el usuario del kiosco inicia sesión (inicio de sesión automático al encender).

  1. Deja la impresora del kiosco como predeterminada (si se configuró).
  2. Muestra "Preparando el cajero..." y espera a que el servidor responda (red, VPN, certificado).
  3. Abre Chrome en modo kiosco. La página hace sola la revisión de arranque (base, cobranzas,
     caja, SIP, worker, impresora) y solo deja cobrar si está todo bien. El PIN es solo para el
     técnico (5 toques en el encabezado).
  4. Si Chrome se cierra o se cuelga, lo vuelve a abrir.

  Para mantenimiento: crear el archivo C:\Kiosco\detener (o cerrar sesión con Ctrl+Alt+Supr).
  No se vuelve a abrir Chrome mientras exista ese archivo.
#>
$ErrorActionPreference = "Continue"
$Base = "C:\Kiosco"
$Log = Join-Path $Base "logs\kiosco-$(Get-Date -Format yyyyMMdd).log"
$Detener = Join-Path $Base "detener"

function Escribir($texto) {
    $linea = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $texto"
    try { Add-Content -Path $Log -Value $linea -Encoding UTF8 } catch {}
}

# --- Configuración (la deja instalar-totem.ps1) ---------------------------------------------
$cfg = Get-Content (Join-Path $Base "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$Url = $cfg.url.TrimEnd("/") + "/"
$UrlInicial = $Url + "?k=" + [uri]::EscapeDataString($cfg.token) + "&impresion=" + $cfg.impresion
Get-ChildItem (Join-Path $Base "logs") -Filter *.log -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force -ErrorAction SilentlyContinue
Escribir "=== Inicio en $env:COMPUTERNAME como $env:USERNAME"

# --- Impresora predeterminada -------------------------------------------------------------------
# Windows cambia la predeterminada a "la última usada" si no se apaga esta opción (es por usuario).
Set-ItemProperty "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Windows" -Name LegacyDefaultPrinterMode -Value 1 -Type DWord -ErrorAction SilentlyContinue
if ($cfg.impresora) {
    $imp = Get-CimInstance Win32_Printer -Filter "Name='$($cfg.impresora -replace "'", "''")'" -ErrorAction SilentlyContinue
    if ($imp) {
        Invoke-CimMethod -InputObject $imp -MethodName SetDefaultPrinter | Out-Null
        Escribir "Impresora predeterminada: $($cfg.impresora) (estado Windows: $($imp.PrinterStatus), sin conexión: $($imp.WorkOffline))"
    } else {
        Escribir "AVISO: no está instalada la impresora '$($cfg.impresora)'"
    }
}

# --- Pantalla de espera ----------------------------------------------------------------------
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$form = New-Object Windows.Forms.Form
$form.FormBorderStyle = "None"; $form.WindowState = "Maximized"; $form.TopMost = $true
$form.BackColor = [Drawing.Color]::FromArgb(11, 37, 69); $form.Cursor = [Windows.Forms.Cursors]::WaitCursor
$titulo = New-Object Windows.Forms.Label
$titulo.Text = "Preparando el cajero..."; $titulo.ForeColor = "White"
$titulo.Font = New-Object Drawing.Font("Segoe UI", 40, [Drawing.FontStyle]::Bold)
$titulo.Dock = "Fill"; $titulo.TextAlign = "MiddleCenter"
$detalle = New-Object Windows.Forms.Label
$detalle.ForeColor = [Drawing.Color]::FromArgb(180, 200, 225); $detalle.Font = New-Object Drawing.Font("Segoe UI", 20)
$detalle.Dock = "Bottom"; $detalle.Height = 160; $detalle.TextAlign = "MiddleCenter"
$form.Controls.Add($titulo); $form.Controls.Add($detalle)

function Mostrar($texto) {
    $detalle.Text = $texto
    if (-not $form.Visible) { $form.Show() }
    [Windows.Forms.Application]::DoEvents()
}

function Esperar($segundos) {
    $fin = (Get-Date).AddSeconds($segundos)
    while ((Get-Date) -lt $fin) { [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 200 }
}

# --- Esperar al servidor -------------------------------------------------------------------------
# Sin esto, si Chrome abre antes de que haya red muestra su página de error y se queda ahí.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$intento = 0
while ($true) {
    if (Test-Path $Detener) { Escribir "Existe ${Detener}: no se abre el kiosco."; $form.Close(); exit 0 }
    $intento++
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 8
        if ($r.StatusCode -eq 200) { Escribir "Servidor responde ($intento intento(s))"; break }
        $motivo = "respuesta $($r.StatusCode)"
    } catch {
        $motivo = $_.Exception.Message
        if ($_.Exception.Response) { $motivo = "HTTP $([int]$_.Exception.Response.StatusCode)" }
    }
    if ($intento -eq 1 -or $intento % 12 -eq 0) { Escribir "Esperando al servidor: $motivo" }
    $ayuda = if ($motivo -match "403") { "El servidor no reconoce este equipo (IP fuera de KIOSCO_REDES)." }
             elseif ($motivo -match "confianza|trust|SSL|TLS") { "Falta el certificado del servidor en este equipo." }
             else { "Esperando conexión con el servidor..." }
    Mostrar "$ayuda`nIntento $intento"
    Esperar 5
}
Mostrar "Abriendo..."

# --- Chrome en modo kiosco, y volver a abrirlo si se cierra --------------------------------------
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { Escribir "ERROR: no está instalado Chrome"; Mostrar "Falta instalar Google Chrome."; Esperar 3600; exit 1 }

$perfil = Join-Path $env:LOCALAPPDATA "KioscoCESSA\chrome"
$argumentos = @(
    "--kiosk", "--kiosk-printing", "--user-data-dir=`"$perfil`"",
    "--no-first-run", "--no-default-browser-check", "--noerrdialogs", "--disable-session-crashed-bubble",
    "--disable-pinch", "--overscroll-history-navigation=0", "--disable-features=Translate,TouchpadOverscrollHistoryNavigation",
    "--autoplay-policy=no-user-gesture-required", "--password-store=basic"
)

$primera = $true
$reinicios = @()
while (-not (Test-Path $Detener)) {
    # Un perfil que quedó marcado como "se cerró mal" muestra un aviso: se limpia antes de abrir.
    $prefs = Join-Path $perfil "Default\Preferences"
    if (Test-Path $prefs) {
        try {
            (Get-Content $prefs -Raw -Encoding UTF8) -replace '"exit_type":"Crashed"', '"exit_type":"Normal"' -replace '"exited_cleanly":false', '"exited_cleanly":true' |
                Set-Content $prefs -Encoding UTF8 -NoNewline
        } catch {}
    }
    $destino = if ($primera) { $UrlInicial } else { $Url }
    Escribir "Abriendo Chrome"
    $p = Start-Process -FilePath $chrome -ArgumentList ($argumentos + "`"$destino`"") -PassThru
    if ($primera) { Start-Sleep -Seconds 4; $form.Close(); $primera = $false }
    $p.WaitForExit()
    # Chrome deja procesos hijos: se espera a que no quede ninguno con este perfil.
    do {
        Start-Sleep -Seconds 2
        $quedan = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*KioscoCESSA*" }
    } while ($quedan)
    Escribir "Chrome se cerró"
    $reinicios = @($reinicios | Where-Object { $_ -gt (Get-Date).AddMinutes(-5) }) + (Get-Date)
    if ($reinicios.Count -ge 5) {
        Escribir "Chrome se cerró 5 veces en 5 minutos: se espera 1 minuto"
        Start-Sleep -Seconds 60
    } else {
        Start-Sleep -Seconds 3
    }
}
Escribir "Existe ${Detener}: se deja de abrir el kiosco."
