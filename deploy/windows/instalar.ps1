# Instalador del Puente Sovos para Windows (PowerShell 5.1 o superior).
#
# Instala o actualiza el puente desde el paquete generado con deploy/empaquetar.sh:
#   - Python portátil con sus librerías (no toca otro Python del equipo)
#   - el puente como servicio de Windows (NSSM): arranca solo al encender el equipo
#     y se reinicia si se cae
#   - la base de datos y los archivos ya procesados, con sus rutas ajustadas
#   - el certificado y la configuración, con acceso solo para el sistema y administradores
#
# Se ejecuta con doble clic en INSTALAR.cmd (pide permisos de administrador).

$ErrorActionPreference = "Stop"
$ServiceName = "SovosBridge"

function Paso($n, $texto) { Write-Host ""; Write-Host "[$n] $texto" -ForegroundColor Cyan }
function Ok($texto) { Write-Host "    OK  $texto" -ForegroundColor Green }
function Aviso($texto) { Write-Host "    !!  $texto" -ForegroundColor Yellow }
function Fallo($texto) { Write-Host ""; Write-Host "ERROR: $texto" -ForegroundColor Red; Read-Host "Presiona Enter para cerrar"; exit 1 }

function Preguntar($texto, $defecto) {
    $r = Read-Host "    $texto [$defecto]"
    if ([string]::IsNullOrWhiteSpace($r)) { return $defecto }
    return $r.Trim()
}
function Confirmar($texto, [bool]$defecto) {
    $op = "s/N"
    if ($defecto) { $op = "S/n" }
    $r = Read-Host "    $texto ($op)"
    if ([string]::IsNullOrWhiteSpace($r)) { return $defecto }
    return $r.Trim().ToLower().StartsWith("s")
}

function Leer-Env($path, $key) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    foreach ($l in [IO.File]::ReadAllLines($path)) {
        if ($l -match "^\s*$([regex]::Escape($key))=(.*)$") { return $Matches[1].Trim() }
    }
    return $null
}
function Poner-Env($path, $key, $value) {
    $lines = @([IO.File]::ReadAllLines($path))
    $found = $false
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($l in $lines) {
        if ($l -match "^\s*$([regex]::Escape($key))=") { $out.Add("$key=$value"); $found = $true } else { $out.Add($l) }
    }
    if (-not $found) { $out.Add("$key=$value") }
    [IO.File]::WriteAllLines($path, $out.ToArray(), (New-Object Text.UTF8Encoding($false)))
}

function Copiar-Carpeta($origen, $destino) {
    # robocopy: códigos menores a 8 son éxito
    robocopy $origen $destino /E /NFL /NDL /NJH /NJS /NP /R:2 /W:2 | Out-Null
    if ($LASTEXITCODE -ge 8) { Fallo "No se pudo copiar $origen a $destino (robocopy $LASTEXITCODE)" }
}

function Proteger($path) {
    # Solo SYSTEM (S-1-5-18) y Administradores (S-1-5-32-544): nombres válidos en Windows en cualquier idioma.
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

# --- 0. permisos de administrador ---------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    exit
}

$Pkg = $PSScriptRoot
Write-Host "=====================================================================" -ForegroundColor Cyan
Write-Host "  Puente Sovos - instalación en este servidor" -ForegroundColor Cyan
Write-Host "  Paquete: $Pkg"
Write-Host "====================================================================="

foreach ($req in @("python\python.exe", "app\sovos_bridge\__main__.py", "nssm.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $Pkg $req))) { Fallo "Falta $req en el paquete. Descomprime el ZIP completo." }
}

# --- 1. carpeta de instalación ------------------------------------------------
Paso 1 "Carpeta de instalación"
$Base = Preguntar "Carpeta del puente" "C:\sovos_bridge"
if ($Base -match "\s") { Fallo "La carpeta no debe tener espacios (el servicio de Windows no la tomaría bien). Usa, por ejemplo, C:\sovos_bridge" }
$Work = Join-Path $Base "work"
$EnvFile = Join-Path $Base ".env"
$Py = Join-Path $Base "python\python.exe"
$Nssm = Join-Path $Base "nssm.exe"
$actualizacion = Test-Path -LiteralPath (Join-Path $Work "db\sovos_bridge.db")
if ($actualizacion) { Aviso "Ya hay un puente instalado en $($Base); se actualizará." }
New-Item -ItemType Directory -Force -Path $Base, $Work | Out-Null

# --- 2. detener lo que esté corriendo -----------------------------------------
Paso 2 "Deteniendo el puente anterior (si existe)"
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne "Stopped") { Stop-Service -Name $ServiceName -Force; Start-Sleep -Seconds 3 }
    & (Join-Path $Pkg "nssm.exe") remove $ServiceName confirm | Out-Null
    Ok "Servicio anterior detenido y retirado"
} else { Ok "No había servicio instalado" }

# --- 3. programa --------------------------------------------------------------
Paso 3 "Copiando Python portátil y el puente"
Copiar-Carpeta (Join-Path $Pkg "python") (Join-Path $Base "python")
if (Test-Path (Join-Path $Base "app")) { Remove-Item -Recurse -Force (Join-Path $Base "app") }
Copiar-Carpeta (Join-Path $Pkg "app") (Join-Path $Base "app")
Copy-Item -Force (Join-Path $Pkg "nssm.exe") $Nssm
$v = Correr $Py @("-m", "sovos_bridge", "--version")
if ($v.Code -ne 0) { Fallo "Python no pudo iniciar el puente: $($v.Out)" }
Ok "Puente $($v.Out.Trim())"

# --- 4. datos: base, comprobantes procesados, bandeja --------------------------
Paso 4 "Base de datos y comprobantes ya procesados"
$datos = Join-Path $Pkg "datos\work"
$importar = $true
if ($actualizacion) {
    Aviso "Este servidor ya tiene su propia base de datos."
    $importar = Confirmar "¿Reemplazarla por la del paquete? (si el servidor ya estuvo enviando, responde N)" $false
}
if ($importar -and (Test-Path $datos)) {
    if ($actualizacion) {
        $resp = "$Work" + "_respaldo_" + (Get-Date -Format "yyyyMMdd_HHmmss")
        Copiar-Carpeta $Work $resp
        Ok "Datos anteriores respaldados en $resp"
    }
    # La base del paquete es un solo archivo: los temporales de la anterior la corromperían.
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $Work "db\sovos_bridge.db-wal"), (Join-Path $Work "db\sovos_bridge.db-shm")
    Copiar-Carpeta $datos $Work
    Ok "Base y archivos copiados a $Work"
} else { Ok "Se conservan los datos del servidor" }

# --- 5. configuración y certificado -------------------------------------------
Paso 5 "Configuración y certificado"
$secretos = Join-Path $Pkg "datos\secretos"
if (-not (Test-Path -LiteralPath $EnvFile)) {
    if (-not (Test-Path (Join-Path $secretos "env"))) { Fallo "El paquete no trae la configuración (datos\secretos\env)" }
    Copy-Item (Join-Path $secretos "env") $EnvFile
    Ok "Configuración instalada"
} else { Ok "Se conserva la configuración existente ($EnvFile)" }

$CertDir = Join-Path $Base "cert"
New-Item -ItemType Directory -Force -Path $CertDir | Out-Null
$certPkg = Get-ChildItem -Path $secretos -Include *.p12, *.pfx -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($certPkg) {
    Copy-Item -Force $certPkg.FullName (Join-Path $CertDir $certPkg.Name)
    Poner-Env $EnvFile "CERT_PATH" (Join-Path $CertDir $certPkg.Name)
    Ok "Certificado instalado en $CertDir"
}

# Carpeta donde el sistema Java deja los TXT
$inboxActual = Leer-Env $EnvFile "INBOX_DIR"
if (-not $inboxActual -or $inboxActual.StartsWith("/")) { $inboxActual = "C:\facturacion\salida_txt" }
Write-Host ""
Write-Host "    ¿En qué carpeta deja los TXT el sistema de facturación (Java)?"
$Inbox = Preguntar "Carpeta de TXT" $inboxActual
if ($Inbox.StartsWith("\\")) {
    Aviso "Es una carpeta de red. El servicio corre como SYSTEM y puede no tener acceso a ella;"
    Aviso "si al final no toma los TXT, configura en Servicios > Puente Sovos > Iniciar sesión un usuario con acceso."
} else { New-Item -ItemType Directory -Force -Path $Inbox | Out-Null }

Poner-Env $EnvFile "WORK_DIR" $Work
Poner-Env $EnvFile "INBOX_DIR" $Inbox
Poner-Env $EnvFile "DRY_RUN" "false"

# Panel web: solo en este servidor o también desde otras PC
Write-Host ""
$enRed = Confirmar "¿Abrir el panel también desde otras PC de la red? (pedirá usuario y clave)" $false
$port = Leer-Env $EnvFile "UI_PORT"
if (-not $port) { $port = "8765" }
if ($enRed) {
    $sec = Read-Host "    Clave para entrar al panel (usuario: admin)" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    if ($plain.Length -lt 8) { Fallo "La clave del panel debe tener al menos 8 caracteres" }
    if ($plain.Contains("'")) { Fallo "La clave del panel no puede llevar comilla simple (')" }
    Poner-Env $EnvFile "UI_HOST" "0.0.0.0"
    Poner-Env $EnvFile "UI_USER" "admin"
    Poner-Env $EnvFile "UI_PASSWORD" "'$plain'"     # entre comillas simples: $ y # se toman literal
    $plain = $null
    Get-NetFirewallRule -DisplayName "Puente Sovos (panel)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "Puente Sovos (panel)" -Direction Inbound -Protocol TCP -LocalPort $port `
        -Action Allow -Profile Domain, Private | Out-Null
    Ok "Panel abierto en la red (puerto $port, solo redes privadas o de dominio)"
} else {
    Poner-Env $EnvFile "UI_HOST" "127.0.0.1"
    Ok "Panel solo en este servidor"
}

Proteger $EnvFile
Proteger $CertDir
Ok "Configuración y certificado accesibles solo para el sistema y administradores"

# --- 6. rutas de la base ------------------------------------------------------
Paso 6 "Ajustando las rutas guardadas en la base"
$origen = Join-Path $Pkg "datos\origen.txt"
if ($importar -and (Test-Path $origen)) {
    $desde = ([IO.File]::ReadAllText($origen)).Trim()
    $r6 = Correr $Py @("-m", "sovos_bridge", "--env", $EnvFile, "reubicar", "--desde", $desde)
    if ($r6.Code -ne 0) { Fallo "No se pudieron ajustar las rutas: $($r6.Out)" }
    Ok (($r6.Out -split "`n") | Where-Object { $_ -like "Rutas*" } | Select-Object -Last 1)
} else { Ok "Sin cambios" }

# --- 7. TXT que ya estaban en la carpeta del Java ------------------------------
Paso 7 "Revisando la carpeta de TXT"
if (-not $Inbox.StartsWith("\\") -or (Test-Path $Inbox)) {
    $previos = @(Get-ChildItem -LiteralPath $Inbox -Filter *.txt -File -ErrorAction SilentlyContinue)
    if ($previos.Count -gt 0) {
        Aviso "Hay $($previos.Count) TXT en $Inbox."
        Aviso "Si el servicio arranca así, los enviaría todos de inmediato, sin pasar por la bandeja"
        Aviso "(sin separar inafectos ni corregir fechas vencidas)."
        if (Confirmar "¿Moverlos a una carpeta aparte para subirlos desde el panel? (recomendado)" $true) {
            $pend = Join-Path $Base ("pendientes_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
            New-Item -ItemType Directory -Force -Path $pend | Out-Null
            $previos | Move-Item -Destination $pend
            Ok "Movidos a $pend. Súbelos desde el panel: Subir TXT y suelta esa carpeta."
        }
    } else { Ok "La carpeta está vacía" }
}
# Lo que en la PC anterior ya estaba liberado hacia la cola de envío
$cola = Join-Path $Pkg "datos\cola"
$enCola = @(Get-ChildItem -LiteralPath $cola -Filter *.txt -File -ErrorAction SilentlyContinue)
if ($importar -and $enCola.Count -gt 0) {
    $enCola | Copy-Item -Destination $Inbox
    Ok "$($enCola.Count) TXT que ya estaban en cola en la PC anterior pasan a la cola de este servidor"
}

# --- 8. servicio de Windows ---------------------------------------------------
Paso 8 "Instalando el servicio (arranca solo al encender el equipo)"
$logs = Join-Path $Work "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
& $Nssm install $ServiceName $Py "-m sovos_bridge --env $EnvFile run" | Out-Null
& $Nssm set $ServiceName AppDirectory (Join-Path $Base "app") | Out-Null
& $Nssm set $ServiceName DisplayName "Puente Sovos - Facturación electrónica" | Out-Null
& $Nssm set $ServiceName Description "Envía a Sovos/SUNAT los comprobantes del sistema de facturación. Panel: http://localhost:$port" | Out-Null
& $Nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START | Out-Null
& $Nssm set $ServiceName AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8" | Out-Null
& $Nssm set $ServiceName AppStdout (Join-Path $logs "servicio_consola.log") | Out-Null
& $Nssm set $ServiceName AppStderr (Join-Path $logs "servicio_consola.log") | Out-Null
& $Nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $Nssm set $ServiceName AppRotateBytes 10485760 | Out-Null
& $Nssm set $ServiceName AppStopMethodConsole 30000 | Out-Null
& $Nssm set $ServiceName AppExit Default Restart | Out-Null
& $Nssm set $ServiceName AppRestartDelay 10000 | Out-Null
# Si Windows lo detiene por un fallo, que lo vuelva a levantar
sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/30000/restart/60000 | Out-Null
Ok "Servicio '$ServiceName' con inicio automático (retrasado, para esperar la red)"

# Que el servidor no se suspenda: si duerme, no envía.
if (Confirmar "¿Evitar que este equipo se suspenda o hiberne? (recomendado)" $true) {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    Ok "Suspensión desactivada con corriente"
}

# Acceso directo al panel en el escritorio de todos los usuarios
$url = Join-Path ([Environment]::GetFolderPath("CommonDesktopDirectory")) "Panel Puente Sovos.url"
[IO.File]::WriteAllLines($url, @("[InternetShortcut]", "URL=http://localhost:$port/"))
Ok "Acceso directo 'Panel Puente Sovos' en el escritorio"

# --- 9. arranque y verificación -----------------------------------------------
Paso 9 "Iniciando y verificando"
try { Start-Service -Name $ServiceName } catch { Aviso "Windows no pudo iniciar el servicio: $($_.Exception.Message)" }
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/summary" -TimeoutSec 3
        if ($r.servicio.activo) { $ok = $true; break }
    } catch { }
}
if (-not $ok) {
    Aviso "El panel no respondió todavía. Revisa $logs\servicio_consola.log"
} else {
    Ok "Servicio activo, ambiente: $($r.ambiente)"
    $hoy = Get-ChildItem -Path $logs -Filter "*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 3
    $firma = $hoy | Select-String -Pattern "Firma propia activa|certificado" -SimpleMatch:$false | Select-Object -Last 1
    if ($firma) { Ok ("Certificado: " + ($firma.Line -replace ".*(vence [0-9/]+).*", '$1')) }
    Start-Process "http://localhost:$port/"
}

# --- 10. limpieza ---------------------------------------------------------------
if (Test-Path $secretos) {
    Write-Host ""
    if (Confirmar "¿Borrar del paquete descomprimido la copia del certificado y la configuración? (recomendado)" $true) {
        Remove-Item -Recurse -Force $secretos
        Ok "Copia borrada. Borra también el ZIP original y vacía la papelera."
    }
}

Write-Host ""
Write-Host "=====================================================================" -ForegroundColor Green
Write-Host "  Listo. El puente arranca solo cada vez que se enciende el equipo." -ForegroundColor Green
Write-Host "  Panel:      http://localhost:$port/" -ForegroundColor Green
Write-Host "  Carpeta:    $Base" -ForegroundColor Green
Write-Host "  Base:       $Work\db\sovos_bridge.db" -ForegroundColor Green
Write-Host "  Servicio:   services.msc > 'Puente Sovos - Facturación electrónica'" -ForegroundColor Green
Write-Host "=====================================================================" -ForegroundColor Green
Read-Host "Presiona Enter para cerrar"
