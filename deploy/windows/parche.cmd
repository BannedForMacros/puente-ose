@echo off
rem Actualizacion del Puente Sovos en un solo archivo: doble clic y listo.
rem No usa PowerShell (algunas PC lo bloquean): el ZIP en Base64 va despues de "exit /b"
rem y lo extrae el Python del propio puente. La base de datos y la configuracion no se tocan.
setlocal
title Puente Sovos - actualizacion
set "BASE=C:\sovos_bridge"
set "SERVICIO=SovosBridge"

net session >nul 2>&1
if errorlevel 1 (
  rem Sin permisos de administrador: se vuelve a abrir pidiendolos
  powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
  if errorlevel 1 (
    echo.
    echo   Haz clic derecho sobre este archivo y elige "Ejecutar como administrador".
    echo.
    pause
  )
  exit /b
)

echo.
echo   Actualizando el Puente Sovos, espera un momento...
echo.
if not exist "%BASE%\python\python.exe" (
  echo   No se encontro el puente en %BASE%.
  goto :fallo
)

net stop %SERVICIO% >nul 2>&1
"%BASE%\python\python.exe" -c "import sys,base64,io,zipfile; t=open(sys.argv[1],'rb').read(); m=b'__PARCHE_'+b'PUENTE__'; z=zipfile.ZipFile(io.BytesIO(base64.b64decode(t[t.rindex(m)+len(m):]))); z.extractall(sys.argv[2]); print('  Programa actualizado:', len(z.namelist()), 'archivos')" "%~f0" "%BASE%\app"
if errorlevel 1 (
  net start %SERVICIO% >nul 2>&1
  goto :fallo
)
net start %SERVICIO% >nul 2>&1
sc query %SERVICIO% | find "RUNNING" >nul
if errorlevel 1 (
  echo   El programa se actualizo pero el servicio no arranco.
  goto :fallo
)
echo   Servicio reiniciado.
echo.
echo   LISTO. Recarga la pagina del panel: http://puente-sovos/
echo.
pause
exit /b

:fallo
echo.
echo   La actualizacion no se completo. Toma una captura de esta ventana y enviala a MacSoft.
echo.
pause
exit /b
