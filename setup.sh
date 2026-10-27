#!/usr/bin/env bash
# ==============================================================================
# Open Drive System — Asistente de instalación y configuración desde cero
# ==============================================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

echo "========================================================"
echo "  Open Drive System · Setup Inicial"
echo "========================================================"

# 1. Entorno virtual de Python
if [ ! -d ".venv" ]; then
  echo "→ Creando entorno virtual de Python (.venv)..."
  python3 -m venv .venv
fi
echo "→ Instalando dependencias de Python..."
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

# 2. Verificar credentials.json
if [ ! -f "credentials.json" ]; then
  echo ""
  echo "⚠️  Aún no existe 'credentials.json' en $BASE_DIR"
  echo "   Sigue el Paso 1 del README.md para descargarlo desde Google Cloud Console"
  echo "   y guárdalo como: $BASE_DIR/credentials.json"
  exit 1
fi

# 3. Autorizar cuentas si no existen
mkdir -p tokens
chmod 700 tokens

for ACC in cuenta1 cuenta2; do
  if [ ! -f "tokens/${ACC}.json" ]; then
    read -r -p "¿Deseas autorizar ahora '${ACC}' en el navegador? [s/N]: " RESP
    if [[ "$RESP" =~ ^[sS]$ ]]; then
      ./.venv/bin/python drivedata.py add-account "$ACC"
    fi
  else
    echo "✓ Token encontrado: tokens/${ACC}.json"
  fi
done

# 4. Primera sincronización local
echo ""
echo "→ Sincronizando inventario local en SQLite (drive.db)..."
./.venv/bin/python drivedata.py sync

# 5. Generar cloud/.secrets.json automáticamente si el usuario desea desplegar en Cloudflare
echo ""
read -r -p "¿Deseas generar 'cloud/.secrets.json' para el panel móvil en Cloudflare? [s/N]: " GEN_CLOUD
if [[ "$GEN_CLOUD" =~ ^[sS]$ ]]; then
  read -r -p "Elige un PIN numérico de acceso para tu celular (ej. 6 dígitos): " USER_PIN
  USER_PIN="${USER_PIN:-123456}"

  ./.venv/bin/python - "$USER_PIN" <<'PY'
import json, pathlib, sys

pin = sys.argv[1]
base = pathlib.Path(".")
creds_raw = json.loads((base / "credentials.json").read_text())
installed = creds_raw.get("installed") or creds_raw.get("web") or {}

def get_rt(name):
    p = base / "tokens" / f"{name}.json"
    if not p.exists():
        return ""
    return json.loads(p.read_text()).get("refresh_token", "")

secrets = {
    "PANEL_PIN": pin,
    "GOOGLE_CLIENT_ID": installed.get("client_id", ""),
    "GOOGLE_CLIENT_SECRET": installed.get("client_secret", ""),
    "REFRESH_TOKEN_CUENTA1": get_rt("cuenta1"),
    "REFRESH_TOKEN_CUENTA2": get_rt("cuenta2"),
}

out = base / "cloud" / ".secrets.json"
out.write_text(json.dumps(secrets, indent=2))
out.chmod(0o600)
print(f"✓ Archivo generado de forma segura en: {out} (permisos 600)")
PY
fi

echo ""
echo "✅ Configuración lista."
echo "   • Para abrir el panel local en tu Mac: ./.venv/bin/python dashboard.py"
echo "   • Para desplegar en Cloudflare Workers: ver Paso 4 en README.md"
