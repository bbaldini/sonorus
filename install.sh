#!/usr/bin/env bash
# Installa Sonorus su Linux o macOS: ambiente Python, modello Whisper e collegamento
# (menu delle applicazioni su Linux, ~/Applications/Sonorus.app su macOS). Si può rilanciare per aggiornare.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Serve Python 3.10 o più recente (macOS: https://www.python.org/downloads/ ; Linux: il pacchetto python3 e python3-venv)."
  exit 1
fi
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
[ -f sonorus.ico ] || .venv/bin/python tools/make_icon.py
.venv/bin/python - <<'PY'
from PIL import Image
im = Image.open("sonorus.ico"); im.size = max(im.info.get("sizes", [im.size])); im.load()
im.convert("RGBA").resize((512, 512)).save("sonorus.png")
PY
if [ "${SONORUS_SKIP_MODEL:-0}" != 1 ]; then  # la CI salta i 1,6 GB del modello
  echo "Scarico il modello di trascrizione (una volta sola, 0,5–1,6 GB)…"
  if [ "$(uname)" = "Darwin" ]; then MODEL=small; else MODEL=large-v3-turbo; fi
  .venv/bin/python -c "import engine; from faster_whisper import WhisperModel; WhisperModel('$MODEL', device='cpu', compute_type='int8', download_root=str(engine.MODEL_DIR))"
fi

if [ "$(uname)" = "Darwin" ]; then
  APP="$HOME/Applications/Sonorus.app"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cat > "$APP/Contents/MacOS/Sonorus" <<SH
#!/bin/bash
cd "$DIR" && exec "$DIR/.venv/bin/python" "$DIR/app.py"
SH
  chmod +x "$APP/Contents/MacOS/Sonorus"
  .venv/bin/python -c "from PIL import Image; Image.open('sonorus.png').save('$APP/Contents/Resources/Sonorus.icns')"
  cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Sonorus</string>
  <key>CFBundleIdentifier</key><string>io.github.bbaldini.sonorus</string>
  <key>CFBundleExecutable</key><string>Sonorus</string>
  <key>CFBundleIconFile</key><string>Sonorus.icns</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSMicrophoneUsageDescription</key><string>Sonorus trascrive la tua voce durante le riunioni, sul tuo computer.</string>
</dict></plist>
PL
  echo
  echo "Fatto: Sonorus è in ~/Applications. Per sentire gli altri serve BlackHole (gratuito): vedi il README."
else
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/sonorus.desktop" <<DE
[Desktop Entry]
Type=Application
Name=Sonorus
Comment=Trascrizione delle riunioni in locale
Exec="$DIR/.venv/bin/python" "$DIR/app.py"
Path=$DIR
Icon=$DIR/sonorus.png
Terminal=false
Categories=AudioVideo;Office;
DE
  echo
  echo "Fatto: trovi Sonorus nel menu delle applicazioni."
fi
