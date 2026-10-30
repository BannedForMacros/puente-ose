#!/usr/bin/env bash
# Arma el paquete de instalación para el servidor Windows con todo lo de esta PC:
# programa, Python portátil con sus librerías, NSSM, la base de datos (copia
# consistente) con los comprobantes ya procesados, la bandeja, el certificado y la
# configuración. Resultado: dist/PuenteSovos_AAAAMMDD_HHMM.zip
#
# Uso:  deploy/empaquetar.sh            (pregunta si detener el puente de esta PC)
#       DETENER=si deploy/empaquetar.sh  (lo detiene sin preguntar: migración definitiva)
#       DETENER=no deploy/empaquetar.sh  (no lo detiene: paquete de prueba)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$ROOT/deploy/cache"
PY_VER="3.12.10"
ENV_FILE="$ROOT/.env"
PIP="$ROOT/.venv/bin/python -m pip"
# Mismas versiones con las que se probó el puente
REQS=(httpx==0.28.1 lxml==6.1.3 pydantic==2.14.2 pydantic_core==2.50.2 python-dotenv==1.2.4
      cryptography==50.0.2 cffi==2.1.1 pycparser==3.12 anyio==4.15.1 certifi==2026.10.23 httpcore==1.0.9
      h11==0.16.0 idna==3.20 typing_extensions==4.16.0 typing-inspection==0.4.4 annotated-types==0.8.0)

env_val() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- ; }
paso() { printf '\n== %s\n' "$1"; }

[ -f "$ENV_FILE" ] || { echo "No existe $ENV_FILE"; exit 1; }
if [ -n "$(git -C "$ROOT" status --porcelain -- sovos_bridge deploy pyproject.toml)" ] && [ "${FORZAR:-}" != "si" ]; then
  echo "Hay cambios sin commit en el programa; el paquete lleva solo lo confirmado (git HEAD)."
  echo "Haz commit primero o ejecuta con FORZAR=si."
  exit 1
fi

WORK="$(env_val WORK_DIR)"; INBOX="$(env_val INBOX_DIR)"; CERT="$(env_val CERT_PATH)"
[ -f "$WORK/db/sovos_bridge.db" ] || { echo "No se encontró la base en $WORK/db"; exit 1; }

# --- detener el puente de esta PC -------------------------------------------------
DETENER="${DETENER:-}"
if [ -z "$DETENER" ]; then
  echo "Para que la copia quede completa y no haya dos puentes enviando a la vez,"
  read -r -p "¿detener el puente de esta PC? El servidor tomará el relevo (s/N): " DETENER
fi
case "$DETENER" in
  s|si|S|SI) pkill -f "sovos_bridge --env .*run" && echo "Puente de esta PC detenido" || echo "No estaba corriendo"
             sleep 2 ;;
  *) echo "AVISO: el puente de esta PC sigue corriendo. Lo que haga después no irá en este paquete." ;;
esac

STAMP="$(date +%Y%m%d_%H%M)"
STAGE="$(mktemp -d)/PuenteSovos"
mkdir -p "$STAGE" "$ROOT/dist" "$CACHE/wheels"
trap 'rm -rf "$(dirname "$STAGE")"' EXIT

# --- componentes de Windows (se descargan una vez) ----------------------------------
paso "Componentes de Windows"
EMBED="$CACHE/python-$PY_VER-embed-amd64.zip"
[ -f "$EMBED" ] || curl -sSfL -o "$EMBED" "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"
[ -f "$CACHE/nssm-2.24.zip" ] || curl -sSfL -o "$CACHE/nssm-2.24.zip" "https://nssm.cc/release/nssm-2.24.zip"
$PIP download -q --dest "$CACHE/wheels" --platform win_amd64 --python-version 3.12 --implementation cp \
     --only-binary=:all: --no-deps "${REQS[@]}"

paso "Python portátil"
PYDIR="$STAGE/python"
mkdir -p "$PYDIR/Lib/site-packages"
unzip -q "$EMBED" -d "$PYDIR"
for w in "${REQS[@]}"; do
  name="$(echo "${w%%==*}" | tr '-' '_')"; ver="${w##*==}"
  whl="$(ls "$CACHE/wheels/" | grep -i -E "^${name}-${ver}-" | grep -E "(none-any|win_amd64)\.whl$" | head -1)"
  [ -n "$whl" ] || { echo "Falta la librería $w para Windows"; exit 1; }
  unzip -q -o "$CACHE/wheels/$whl" -d "$PYDIR/Lib/site-packages"
done
# El Python portátil ignora PYTHONPATH: sus rutas se fijan en el ._pth
printf 'python312.zip\r\n.\r\nLib\\site-packages\r\n..\\app\r\nimport site\r\n' > "$PYDIR/python312._pth"
unzip -q -j "$CACHE/nssm-2.24.zip" "nssm-2.24/win64/nssm.exe" -d "$STAGE"

paso "Programa (git HEAD $(git -C "$ROOT" rev-parse --short HEAD))"
mkdir -p "$STAGE/app"
git -C "$ROOT" archive HEAD sovos_bridge pyproject.toml requirements.txt README.md GUIA_OPERACION.md FICHA_TECNICA.md \
  | tar -x -C "$STAGE/app"

paso "Base de datos y comprobantes"
DATOS="$STAGE/datos"
mkdir -p "$DATOS/work/db" "$DATOS/cola" "$DATOS/secretos"
sqlite3 "$WORK/db/sovos_bridge.db" ".backup '$DATOS/work/db/sovos_bridge.db'"
sqlite3 "$DATOS/work/db/sovos_bridge.db" "PRAGMA integrity_check;" | grep -qx ok || { echo "La copia de la base no pasó la verificación"; exit 1; }
rsync -a --exclude 'db/' --exclude 'logs/' --exclude 'cert/' --exclude 'inbox/' --exclude 'dry_run/' \
      --exclude 'heartbeat' --exclude '*_respaldo_*' --exclude '.DS_Store' "$WORK/" "$DATOS/work/"
if [ -d "$INBOX" ]; then find "$INBOX" -maxdepth 1 -name '*.txt' -exec cp {} "$DATOS/cola/" \; ; fi
printf '%s' "$WORK" > "$DATOS/origen.txt"

paso "Configuración y certificado"
cp "$ENV_FILE" "$DATOS/secretos/env"
if [ -n "$CERT" ] && [ -f "$CERT" ]; then cp "$CERT" "$DATOS/secretos/$(basename "$CERT")"; fi

paso "Instalador"
# PowerShell 5.1 necesita UTF-8 con BOM para leer tildes; Windows prefiere CRLF.
{ printf '\xef\xbb\xbf'; sed 's/$/\r/' "$ROOT/deploy/windows/instalar.ps1"; } > "$STAGE/instalar.ps1"
cp "$ROOT/deploy/windows/INSTALAR.cmd" "$STAGE/"
sed 's/$/\r/' "$ROOT/deploy/windows/LEEME.txt" > "$STAGE/LEEME.txt"

OUT="$ROOT/dist/PuenteSovos_$STAMP.zip"
(cd "$(dirname "$STAGE")" && zip -qr -X "$OUT" PuenteSovos)
chmod 600 "$OUT"

DOCS=$(sqlite3 "$DATOS/work/db/sovos_bridge.db" "select count(*) from documentos")
TRAY=$(sqlite3 "$DATOS/work/db/sovos_bridge.db" "select count(*) from bandeja where estado<>'EN_COLA'")
COLA=$(find "$DATOS/cola" -name '*.txt' | wc -l | tr -d ' ')
paso "Listo"
echo "Paquete : $OUT ($(du -h "$OUT" | cut -f1))"
echo "Incluye : $DOCS comprobantes, $TRAY en la bandeja, $COLA en cola, certificado y configuración"
echo "IMPORTANTE: lleva el certificado y las claves. Cópialo por USB, no por correo ni WhatsApp."
