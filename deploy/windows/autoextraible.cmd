@echo off
rem Instalador del Puente Sovos en un solo archivo: doble clic y listo.
rem Despues de "exit /b" va el paquete (ZIP en Base64); este bloque lo extrae y ejecuta el instalador.
setlocal
title Puente Sovos - instalacion
set "PS_SELF=%~f0"
echo.
echo   Preparando la instalacion del Puente Sovos, espera un momento...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $t=[IO.File]::ReadAllText($env:PS_SELF); $m='__PAQUETE_'+'PUENTE_SOVOS__'; $i=$t.LastIndexOf($m); if($i -lt 0){ throw 'Archivo incompleto' }; $b=[Convert]::FromBase64String($t.Substring($i+$m.Length)); $d=Join-Path $env:TEMP ('PuenteSovos_'+(Get-Date -Format 'yyyyMMddHHmmss')); New-Item -ItemType Directory -Force -Path $d | Out-Null; $z=Join-Path $d 'paquete.zip'; [IO.File]::WriteAllBytes($z,$b); Add-Type -AssemblyName System.IO.Compression.FileSystem; [IO.Compression.ZipFile]::ExtractToDirectory($z,$d); Remove-Item $z; $q=[char]34; Start-Process powershell.exe -Verb RunAs -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File '+$q+(Join-Path $d 'PuenteSovos\instalar.ps1')+$q)"
if errorlevel 1 (
  echo.
  echo   No se pudo preparar la instalacion. Vuelve a copiar el archivo completo e intentalo de nuevo.
  pause
)
exit /b
__PAQUETE_PUENTE_SOVOS__
