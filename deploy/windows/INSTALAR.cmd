@echo off
rem Instala o actualiza el Puente Sovos. Pide permisos de administrador.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1"
