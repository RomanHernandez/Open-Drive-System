#!/usr/bin/env bash
# ==============================================================================
# Exporta una copia 100% limpia del proyecto (sin credenciales, tokens ni BD)
# lista para compartir con otras personas o subir a GitHub.
# ==============================================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_ZIP="${1:-$BASE_DIR/../open-drive-system-template.zip}"
TMP_DIR="$(mktemp -d)"
STAGE="$TMP_DIR/open-drive-system"

mkdir -p "$STAGE/cloud/src"

# Copiar únicamente código fuente, plantillas y documentación (CERO secretos)
cp "$BASE_DIR/.gitignore" "$STAGE/"
cp "$BASE_DIR/README.md" "$STAGE/"
cp "$BASE_DIR/requirements.txt" "$STAGE/"
cp "$BASE_DIR/drivedata.py" "$STAGE/"
cp "$BASE_DIR/dashboard.py" "$STAGE/"
cp "$BASE_DIR/setup.sh" "$STAGE/"
cp "$BASE_DIR/export-template.sh" "$STAGE/"
cp "$BASE_DIR/credentials.example.json" "$STAGE/"

cp "$BASE_DIR/cloud/.gitignore" "$STAGE/cloud/"
cp "$BASE_DIR/cloud/.secrets.example.json" "$STAGE/cloud/"
cp "$BASE_DIR/cloud/cloudflare.config.ts" "$STAGE/cloud/"
cp "$BASE_DIR/cloud/package.json" "$STAGE/cloud/"
cp "$BASE_DIR/cloud/tsconfig.json" "$STAGE/cloud/"
cp "$BASE_DIR/cloud/vite.config.ts" "$STAGE/cloud/"
cp "$BASE_DIR/cloud/src/index.ts" "$STAGE/cloud/src/"

chmod +x "$STAGE/setup.sh" "$STAGE/export-template.sh" "$STAGE/drivedata.py" "$STAGE/dashboard.py"

rm -f "$OUT_ZIP"
(cd "$TMP_DIR" && zip -qr "$OUT_ZIP" "open-drive-system")
rm -rf "$TMP_DIR"

echo "✅ Paquete limpio exportado en: $OUT_ZIP"
echo "   (Verificado: no contiene credentials.json, tokens/, drive.db ni .secrets.json)"
