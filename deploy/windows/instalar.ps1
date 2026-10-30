# Instalador del Puente Sovos para Windows (PowerShell 5.1 o superior). Sin preguntas.
#
# Lo ejecuta InstalarPuenteSovos.cmd (un solo archivo con todo adentro). Deja:
#   - Python portátil con sus librerías y el puente en C:\sovos_bridge
#   - el servicio de Windows "SovosBridge": arranca solo al encender el equipo
#     (aunque nadie inicie sesión) y se vuelve a levantar si se cae
#   - la base de datos con lo ya procesado (solo en la primera instalación)
#   - el panel en http://puente-sovos/ (puerto 80 si está libre) con clave propia
# El certificado, las credenciales de Sovos y la carpeta de TXT se configuran
# después en el panel (pestaña Configuración): el instalador no lleva secretos.
#
# Parámetros opcionales: -Base D:\otra_carpeta   -ReemplazarDatos (pisa la base del servidor)

param(
    [string]$Base = "C:\sovos_bridge",
    [switch]$ReemplazarDatos
)

$ErrorActionPreference = "Stop"
$ServiceName = "SovosBridge"
$Alias = "puente-sovos"

function Paso($n, $texto) { Write-Host ""; Write-Host "[$n/9] $texto" -ForegroundColor Cyan }
function Ok($texto) { Write-Host "      OK  $texto" -ForegroundColor Green }
function Aviso($texto) { Write-Host "      !!  $texto" -ForegroundColor Yellow }
function Fallo($texto) { Write-Host ""; Write-Host "ERROR: $texto" -ForegroundColor Red; Read-Host "Presiona Enter para cerrar"; exit 1 }

function Leer-Env($path, $key) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    foreach ($l in [IO.File]::ReadAllLines($path)) {
        if ($l -match "^\s*$([regex]::Escape($key))=(.*)$") { return $Matches[1].Trim().Trim("'") }
    }
    return $null
}
function Poner-Env($path, $key, $value) {
    $out = New-Object System.Collections.Generic.List[string]
    $found = $false
    foreach ($l in [IO.File]::ReadAllLines($path)) {
        if ($l -match "^\s*$([regex]::Escape($key))=") { $out.Add("$key=$value"); $found = $true } else { $out.Add($l) }
    }
    if (-not $found) { $out.Add("$key=$value") }
    [IO.File]::WriteAllLines($path, $out.ToArray(), (New-Object Text.UTF8Encoding($false)))
}
function Copiar-Carpeta($origen, $destino) {
    robocopy $origen $destino /E /NFL /NDL /NJH /NJS /NP /R:2 /W:2 | Out-Null
    if ($LASTEXITCODE -ge 8) { Fallo "No se pudo copiar $origen a $destino (robocopy $LASTEXITCODE)" }
}
function Proteger($path) {
    # Solo SYSTEM (S-1-5-18) y Administradores (S-1-5-32-544): válido en Windows en cualquier idioma.
    $perm = "(F)"
    if ((Get-Item -LiteralPath $path).PSIsContainer) { $perm = "(OI)(CI)(F)" }
    icacls $path /inheritance:r /grant:r "*S-1-5-18:$perm" "*S-1-5-32-544:$perm" /T /C /Q | Out-Null
}
# Ejecuta un programa sin que sus mensajes por stderr detengan el instalador (PowerShell 5.1).
function Correr($exe, [string[]]$argumentos) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $exe @argumentos 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return @{ Code = $code; Out = ($out -join "`n") }
}
function Nueva-Clave {
    # Sin caracteres que se confunden al dictarla (0/O, 1/l/I)
    $chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".ToCharArray()
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 12
    $rng.GetBytes($bytes)
    return -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}
function Puerto-Libre($port) {
    return -not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# --- permisos de administrador --------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $args2 = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Base", "`"$Base`"")
    if ($ReemplazarDatos) { $args2 += "-ReemplazarDatos" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $args2
    exit
}

$Pkg = $PSScriptRoot
Write-Host "=====================================================================" -ForegroundColor Cyan
Write-Host "  Puente Sovos - instalación (no hace falta responder nada)" -ForegroundColor Cyan
Write-Host "====================================================================="
foreach ($req in @("python\python.exe", "app\sovos_bridge\__main__.py", "nssm.exe", "datos\env")) {
    if (-not (Test-Path -LiteralPath (Join-Path $Pkg $req))) { Fallo "Falta $req en el paquete." }
}
if ($Base -match "\s") { Fallo "La carpeta de instalación no debe tener espacios: $Base" }

$Work = Join-Path $Base "work"
$EnvFile = Join-Path $Base ".env"
$Py = Join-Path $Base "python\python.exe"
$Nssm = Join-Path $Base "nssm.exe"
$primera = -not (Test-Path -LiteralPath (Join-Path $Work "db\sovos_bridge.db"))
New-Item -ItemType Directory -Force -Path $Base, $Work | Out-Null

# --- 1. detener la versión anterior ---------------------------------------------
Paso 1 "Preparando el equipo"
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne "Stopped") { Stop-Service -Name $ServiceName -Force; Start-Sleep -Seconds 3 }
    Correr (Join-Path $Pkg "nssm.exe") @("remove", $ServiceName, "confirm") | Out-Null
    Ok "Versión anterior detenida (se actualiza conservando datos y configuración)"
} else { Ok "Instalación nueva en $Base" }

# --- 2. programa ----------------------------------------------------------------
Paso 2 "Copiando el programa"
Copiar-Carpeta (Join-Path $Pkg "python") (Join-Path $Base "python")
if (Test-Path (Join-Path $Base "app")) { Remove-Item -Recurse -Force (Join-Path $Base "app") }
Copiar-Carpeta (Join-Path $Pkg "app") (Join-Path $Base "app")
Copy-Item -Force (Join-Path $Pkg "nssm.exe") $Nssm
$v = Correr $Py @("-m", "sovos_bridge", "--version")
if ($v.Code -ne 0) { Fallo "Python no pudo iniciar el puente: $($v.Out)" }
Ok "Puente $($v.Out.Trim())"

# --- 3. base de datos -----------------------------------------------------------
Paso 3 "Base de datos"
$datos = Join-Path $Pkg "datos\work"
$importada = $false
if (($primera -or $ReemplazarDatos) -and (Test-Path (Join-Path $datos "db\sovos_bridge.db"))) {
    if (-not $primera) {
        $resp = "$Work" + "_respaldo_" + (Get-Date -Format "yyyyMMdd_HHmmss")
        Copiar-Carpeta $Work $resp
        Ok "Datos anteriores respaldados en $resp"
    }
    # La base del paquete es un solo archivo: los temporales de una anterior la corromperían.
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $Work "db\sovos_bridge.db-wal"), (Join-Path $Work "db\sovos_bridge.db-shm")
    Copiar-Carpeta $datos $Work
    $importada = $true
    Ok "Base instalada con los comprobantes ya procesados, la bandeja y las constancias"
} else {
    Ok "Se conserva la base de este servidor ($Work\db\sovos_bridge.db)"
}

# --- 4. configuración -----------------------------------------------------------
Paso 4 "Configuración"
if (-not (Test-Path -LiteralPath $EnvFile)) { Copy-Item (Join-Path $Pkg "datos\env") $EnvFile }
Poner-Env $EnvFile "WORK_DIR" $Work
Poner-Env $EnvFile "DRY_RUN" "false"
# Rutas de la PC anterior (Mac/Linux) no sirven aquí: se configuran en el panel.
foreach ($k in @("INBOX_DIR", "CERT_PATH")) {
    $val = Leer-Env $EnvFile $k
    if ($val -and $val.StartsWith("/")) { Poner-Env $EnvFile $k "" }
}
# Si el paquete trae el certificado (solo si se armó con INCLUIR_SECRETOS=si)
$CertDir = Join-Path $Work "cert"
New-Item -ItemType Directory -Force -Path $CertDir | Out-Null
$certPkg = Get-ChildItem -Path (Join-Path $Pkg "datos\secretos") -Include *.p12, *.pfx -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($certPkg) {
    Copy-Item -Force $certPkg.FullName (Join-Path $CertDir $certPkg.Name)
    Poner-Env $EnvFile "CERT_PATH" (Join-Path $CertDir $certPkg.Name)
    Ok "Certificado incluido en el paquete instalado"
}

# Panel: nombre propio, puerto 80 si está libre, abierto a la red con clave
$port = Leer-Env $EnvFile "UI_PORT"
if ($primera -or -not $port -or $port -eq "8765") {
    if (Puerto-Libre 80) { $port = "80" } else { $port = "8765"; Aviso "El puerto 80 está ocupado por otro programa: el panel usará el 8765" }
}
Poner-Env $EnvFile "UI_PORT" $port
Poner-Env $EnvFile "UI_HOST" "0.0.0.0"
Poner-Env $EnvFile "UI_ALIAS" $Alias
$usuarioPanel = Leer-Env $EnvFile "UI_USER"
if (-not $usuarioPanel) { $usuarioPanel = "admin"; Poner-Env $EnvFile "UI_USER" "admin" }
$clavePanel = Leer-Env $EnvFile "UI_PASSWORD"
$claveNueva = $false
if (-not $clavePanel) { $clavePanel = Nueva-Clave; Poner-Env $EnvFile "UI_PASSWORD" $clavePanel; $claveNueva = $true }
Ok "Configuración lista"

# --- 5. rutas de la base --------------------------------------------------------
Paso 5 "Ajustando la base a este equipo"
$origen = Join-Path $Pkg "datos\origen.txt"
if ($importada -and (Test-Path $origen)) {
    $desde = ([IO.File]::ReadAllText($origen)).Trim()
    $r5 = Correr $Py @("-m", "sovos_bridge", "--env", $EnvFile, "reubicar", "--desde", $desde)
    if ($r5.Code -ne 0) { Fallo "No se pudieron ajustar las rutas: $($r5.Out)" }
    Ok (($r5.Out -split "`n") | Where-Object { $_ -like "Rutas*" } | Select-Object -Last 1)
} else { Ok "Sin cambios" }

# --- 6. nombre y red ------------------------------------------------------------
Paso 6 "Nombre del panel y acceso desde la red"
$hosts = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
$tieneAlias = Select-String -LiteralPath $hosts -Pattern "^\s*127\.0\.0\.1\s+$([regex]::Escape($Alias))(\s|$)" -Quiet
if (-not $tieneAlias) {
    Add-Content -LiteralPath $hosts -Value "`r`n127.0.0.1`t$Alias`t# Puente Sovos" -Encoding ASCII
}
Ok "En este servidor: http://$Alias/"
Get-NetFirewallRule -DisplayName "Puente Sovos (panel)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Puente Sovos (panel)" -Direction Inbound -Protocol TCP -LocalPort $port `
    -Action Allow -Profile Domain, Private | Out-Null
Ok "Firewall: panel permitido en la red interna (puerto $port)"

Proteger $EnvFile
Proteger $CertDir
Ok "Configuración y certificado accesibles solo para el sistema y administradores"

# --- 7. servicio ----------------------------------------------------------------
Paso 7 "Servicio de Windows (arranca solo al encender)"
$logs = Join-Path $Work "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$nssmArgs = @(
    @("install", $ServiceName, $Py, "-m sovos_bridge --env $EnvFile run"),
    @("set", $ServiceName, "AppDirectory", (Join-Path $Base "app")),
    @("set", $ServiceName, "DisplayName", "Puente Sovos - Facturación electrónica"),
    @("set", $ServiceName, "Description", "Envía a Sovos/SUNAT los comprobantes del sistema de facturación."),
    @("set", $ServiceName, "Start", "SERVICE_DELAYED_AUTO_START"),
    @("set", $ServiceName, "AppEnvironmentExtra", "PYTHONUTF8=1", "PYTHONIOENCODING=utf-8"),
    @("set", $ServiceName, "AppStdout", (Join-Path $logs "servicio_consola.log")),
    @("set", $ServiceName, "AppStderr", (Join-Path $logs "servicio_consola.log")),
    @("set", $ServiceName, "AppRotateFiles", "1"),
    @("set", $ServiceName, "AppRotateBytes", "10485760"),
    @("set", $ServiceName, "AppStopMethodConsole", "30000"),
    @("set", $ServiceName, "AppExit", "Default", "Restart"),
    @("set", $ServiceName, "AppRestartDelay", "10000")
)
foreach ($a in $nssmArgs) {
    $r7 = Correr $Nssm $a
    if ($r7.Code -ne 0) { Fallo "NSSM no pudo configurar el servicio ($($a -join ' ')): $($r7.Out)" }
}
Correr "sc.exe" @("failure", $ServiceName, "reset=", "86400", "actions=", "restart/10000/restart/30000/restart/60000") | Out-Null
# Si el equipo se suspende, no envía
Correr "powercfg.exe" @("/change", "standby-timeout-ac", "0") | Out-Null
Correr "powercfg.exe" @("/change", "hibernate-timeout-ac", "0") | Out-Null
Ok "Servicio '$ServiceName' con inicio automático; suspensión desactivada"

# --- 8. accesos directos --------------------------------------------------------
Paso 8 "Accesos directos"
$sufijo = ""
if ($port -ne "80") { $sufijo = ":$port" }
$urlLocal = "http://$Alias$sufijo/"
$equipo = $env:COMPUTERNAME.ToLower()
$urlRed = "http://$equipo$sufijo/"
$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
       Select-Object -First 1).IPAddress
$urlIp = ""
if ($ip) { $urlIp = "http://$ip$sufijo/" }
$pub = [Environment]::GetFolderPath("CommonDesktopDirectory")
[IO.File]::WriteAllLines((Join-Path $pub "Panel Puente Sovos.url"), @("[InternetShortcut]", "URL=$urlLocal"))
$lineaIp = ""
if ($urlIp) { $lineaIp = "Si el nombre no responde: $urlIp" }
$ficha = @(
    "PUENTE SOVOS - ACCESO AL PANEL",
    "==============================",
    "",
    "En este servidor:         $urlLocal",
    "Desde otras PC de la red: $urlRed",
    $lineaIp,
    "",
    "Usuario: $usuarioPanel",
    "Clave:   $clavePanel",
    "",
    "Guarda esta clave en un lugar seguro y borra este archivo.",
    "Se puede cambiar en el panel: Configuración > Acceso al panel."
)
$fichaPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Acceso Puente Sovos.txt"
if ($claveNueva -or -not (Test-Path $fichaPath)) { [IO.File]::WriteAllLines($fichaPath, $ficha) }
Ok "Escritorio: 'Panel Puente Sovos' y 'Acceso Puente Sovos.txt' (con la clave)"

# --- 9. arranque ----------------------------------------------------------------
Paso 9 "Iniciando"
try { Start-Service -Name $ServiceName } catch { Aviso "Windows no pudo iniciar el servicio: $($_.Exception.Message)" }
$auth = @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($usuarioPanel):$clavePanel")) }
$ok = $false
$r = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/summary" -Headers $auth -TimeoutSec 3
        $ok = $true
        break
    } catch { }
}
if ($ok) {
    Ok "Servicio activo"
    if ($r.config_pendiente.Count -gt 0) {
        Aviso "Falta completar en el panel (pestaña Configuración):"
        foreach ($p in $r.config_pendiente) { Aviso "  - $($p.texto)" }
    }
    Start-Process $urlLocal
} else {
    Aviso "El panel no respondió todavía. Revisa $logs\servicio_consola.log"
}

Write-Host ""
Write-Host "=====================================================================" -ForegroundColor Green
Write-Host "  LISTO. El puente arranca solo cada vez que se enciende el equipo." -ForegroundColor Green
Write-Host ""
Write-Host "  Panel en este servidor:   $urlLocal" -ForegroundColor Green
Write-Host "  Desde otras PC de la red: $urlRed" -ForegroundColor Green
if ($urlIp) { Write-Host "  Si el nombre no responde: $urlIp" -ForegroundColor Green }
Write-Host "  Usuario: $usuarioPanel    Clave: $clavePanel" -ForegroundColor Green
Write-Host ""
Write-Host "  Siguiente paso: en el panel, pestaña Configuración, carga el certificado," -ForegroundColor Green
Write-Host "  las credenciales de Sovos y la carpeta de TXT." -ForegroundColor Green
Write-Host "=====================================================================" -ForegroundColor Green
# La copia extraída en TEMP lleva la base (datos de pacientes): no se deja ahí.
$tmpPkg = Split-Path -Parent $Pkg
if ((Split-Path -Leaf $tmpPkg) -like "PuenteSovos_*" -and $tmpPkg.StartsWith($env:TEMP, [StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $tmpPkg
}
Read-Host "Presiona Enter para cerrar"
