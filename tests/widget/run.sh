#!/usr/bin/env bash
# Widget uçtan uca testi: sahte sunucuyu başlatır, Playwright testini çalıştırır, sunucuyu kapatır.
# Kullanım: bash tests/widget/run.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${MOCK_PORT:-8765}"
NODE="${NODE_BIN:-/opt/node22/bin/node}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"
export MOCK_BASE="http://127.0.0.1:${PORT}"

python3 -c "import fastapi, uvicorn" 2>/dev/null || pip install -q fastapi uvicorn

python3 "$HERE/mock_server.py" --port "$PORT" &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# Sunucu hazır olana kadar bekle (en fazla ~10 sn)
for _ in $(seq 1 100); do
  if curl -fsS "$MOCK_BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

"$NODE" "$HERE/widget.test.mjs"
