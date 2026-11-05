#!/usr/bin/env bash
# Arma la actualización para el servidor Windows: UN SOLO ARCHIVO
#   dist/ActualizarPuenteSovos_AAAAMMDD_HHMM.cmd
# con el programa de git HEAD (sovos_bridge y schemas). Doble clic en el servidor:
# detiene el servicio, reemplaza el programa y lo vuelve a iniciar. No toca la base,
# la configuración ni el certificado. No lleva datos de pacientes ni claves.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -n "$(git -C "$ROOT" status --porcelain -- sovos_bridge schemas)" ] && [ "${FORZAR:-}" != "si" ]; then
  echo "Hay cambios sin commit en el programa; la actualización lleva solo lo confirmado (git HEAD)."
  echo "Haz commit primero o ejecuta con FORZAR=si."
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git -C "$ROOT" archive --format=zip -o "$TMP/app.zip" HEAD sovos_bridge schemas

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/ActualizarPuenteSovos_$(date +%Y%m%d_%H%M).cmd"
# Windows necesita CRLF en el .cmd; el Base64 va en una sola línea al final
{ sed 's/$/\r/' "$ROOT/deploy/windows/parche.cmd"; printf '__PARCHE_PUENTE__'; base64 < "$TMP/app.zip" | tr -d '\n'; } > "$OUT"

echo "Actualización: $OUT ($(du -h "$OUT" | cut -f1), git $(git -C "$ROOT" rev-parse --short HEAD))"
echo "No lleva datos de pacientes ni claves: se puede enviar por WhatsApp, correo o Drive (comprimida en ZIP)."
