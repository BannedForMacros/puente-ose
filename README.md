# sovos-bridge — TXT → Sovos Compliance Network (Perú)

Servicio que toma los TXT de comprobantes que genera el sistema de facturación
de **Hogar Clínica San Juan de Dios Chiclayo (RUC 20437310841)**, los convierte
al formato SBD de Sovos y los envía a Sovos Compliance Network. Sovos firma,
envía al OSE/SUNAT y devuelve el XML firmado y el PDF.

Reemplaza al puente anterior hacia Nubefact. El XML que se arma es **idéntico**
al que generaba el puente anterior y que SUNAT aceptaba (comprobado nodo por
nodo en `tests/test_ubl_reverse.py`), sin la firma y con la Ñ y tildes bien
escritas.

- Documentación técnica: [FICHA_TECNICA.md](FICHA_TECNICA.md)
- Guía para el personal de TI: [GUIA_OPERACION.md](GUIA_OPERACION.md)

## Requisitos

- Python 3.12 o superior
- Acceso HTTPS a la URL de Sovos
- Lectura/escritura sobre la carpeta donde el sistema Java deja los TXT

## Instalación en Windows (servicio con NSSM)

1. **Python**: instalar Python 3.12 (64 bits) desde python.org marcando
   *Add python.exe to PATH*.

2. **Copiar el proyecto**, por ejemplo a `C:\sovos_bridge\app`.

3. **Entorno e instalación** (PowerShell como administrador):

   ```powershell
   cd C:\sovos_bridge\app
   python -m venv .venv
   .\.venv\Scripts\python -m pip install -r requirements.txt
   .\.venv\Scripts\python -m pytest -q        # deben pasar todas las pruebas
   ```

4. **Configuración**: copiar `.env.example` a `.env` y completar (ver
   [FICHA_TECNICA.md § Configuración](FICHA_TECNICA.md#5-configuración-env)).
   Proteger el archivo, contiene las credenciales:

   ```powershell
   icacls C:\sovos_bridge\app\.env /inheritance:r /grant:r "Administradores:F" "SYSTEM:F"
   ```

5. **Prueba en seco** (genera el SBD sin enviarlo):

   ```powershell
   .\.venv\Scripts\python -m sovos_bridge --env C:\sovos_bridge\app\.env send C:\ruta\un_comprobante.txt --dry-run
   ```

   El SBD queda en `WORK_DIR\dry_run\`.

6. **Instalar el servicio con NSSM** (descargar de https://nssm.cc y copiar
   `nssm.exe` a `C:\sovos_bridge\nssm.exe`):

   ```powershell
   $nssm = "C:\sovos_bridge\nssm.exe"
   & $nssm install SovosBridge "C:\sovos_bridge\app\.venv\Scripts\python.exe" "-m sovos_bridge --env C:\sovos_bridge\app\.env run"
   & $nssm set SovosBridge AppDirectory "C:\sovos_bridge\app"
   & $nssm set SovosBridge DisplayName "Puente Sovos - Facturación electrónica"
   & $nssm set SovosBridge Start SERVICE_AUTO_START
   & $nssm set SovosBridge AppStdout "C:\sovos_bridge\nssm_salida.log"
   & $nssm set SovosBridge AppStderr "C:\sovos_bridge\nssm_salida.log"
   & $nssm set SovosBridge AppRotateFiles 1
   & $nssm set SovosBridge AppRotateBytes 10485760
   & $nssm set SovosBridge AppStopMethodConsole 30000
   & $nssm set SovosBridge AppExit Default Restart
   & $nssm set SovosBridge AppRestartDelay 10000
   & $nssm start SovosBridge
   ```

   Si la bandeja de TXT está en una carpeta de red, el servicio debe correr
   con un usuario que tenga acceso:
   `& $nssm set SovosBridge ObjectName ".\usuario_servicio" "contraseña"`.

   Comandos útiles: `nssm status SovosBridge`, `nssm restart SovosBridge`,
   `nssm stop SovosBridge`, `nssm edit SovosBridge`.

## Instalación en Linux (systemd)

```bash
sudo useradd --system --home /opt/sovos_bridge sovos
sudo mkdir -p /opt/sovos_bridge && sudo cp -r . /opt/sovos_bridge/app
cd /opt/sovos_bridge/app
sudo python3.12 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env       # INBOX_DIR, WORK_DIR, credenciales
sudo chown -R sovos:sovos /opt/sovos_bridge && sudo chmod 600 .env
```

`/etc/systemd/system/sovos-bridge.service`:

```ini
[Unit]
Description=Puente TXT -> Sovos (facturación electrónica)
After=network-online.target
Wants=network-online.target

[Service]
User=sovos
WorkingDirectory=/opt/sovos_bridge/app
ExecStart=/opt/sovos_bridge/app/.venv/bin/python -m sovos_bridge --env /opt/sovos_bridge/app/.env run
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sovos-bridge
systemctl status sovos-bridge
journalctl -u sovos-bridge -f
```

## Comandos

| Comando | Uso |
|---|---|
| `python -m sovos_bridge run` | Servicio continuo (bandeja + consulta de estados) |
| `python -m sovos_bridge send <archivo.txt> [--dry-run]` | Procesa un solo TXT (trabaja sobre una copia) |
| `python -m sovos_bridge status <serie-número> [--consultar]` | Estado e historial, ej. `status BT05-00125304` |
| `python -m sovos_bridge retry-errors [--forzar] [--todos]` | Reintenta errores (ver GUIA_OPERACION.md) |
| `python -m sovos_bridge report --date AAAA-MM-DD` | Resumen del día en CSV |

Con `--env <ruta>` (antes del comando) se indica un `.env` distinto al de la
carpeta actual.

## Pruebas

```bash
python -m pytest -q
```

Cubren: parser con los TXT de `samples/`, encoding cp1252/UTF-8, igualdad con
los XML aceptados por SUNAT, SBD por tipo validado contra el XSD oficial de
Sovos, controles, duplicados, reintentos 408/429/5xx/401 con un servidor Sovos
simulado, flujo completo hasta aprobado/rechazado y enmascarado de logs.

## Estructura

```
sovos_bridge/
  parser/       TXT -> modelo (layout por registro, encoding, corchetes)
  validation/   controles y catálogos SUNAT
  sbd/          UBL 2.1 (ingeniería inversa), sobre SBD, validación XSD, baja (pendiente)
  sovos/        autenticación, cliente HTTP con reintentos, lectura de respuestas
  storage/      SQLite: estados, eventos, notificaciones
  service/      pipeline, consulta de estados, bandeja, servicio, operaciones
  cli.py        línea de comandos
schemas/sovos/  XSD oficiales del SBD de Sovos (v4.1)
samples/        TXT, XML y CDR reales anonimizados
tests/          pruebas (pytest); tests/data = TXT reconstruidos desde XML
```
