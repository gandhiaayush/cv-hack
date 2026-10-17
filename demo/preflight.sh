#!/usr/bin/env bash
# Pre-stage checklist. Run this before presenting; every line should say OK.
#   demo/preflight.sh
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
fails=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails+1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }

echo "Preflight"
[ -x "$PY" ] && ok "venv" || { bad "venv missing: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
$PY -c "import fastapi, uvicorn, pydantic, openai, dotenv" 2>/dev/null && ok "python deps" || bad "python deps: .venv/bin/pip install -r requirements.txt"

out=$($PY -m pytest -q 2>&1 | tail -1); [[ "$out" == *passed* && "$out" != *failed* ]] && ok "tests: $out" || bad "tests: $out"

(cd dashboard/web && { [ -d node_modules ] || npm install --no-audit --no-fund >/dev/null 2>&1; } && npm run build >/dev/null 2>&1) \
  && ok "frontend build" || bad "frontend build (cd dashboard/web && npm run build)"

if [ -z "${KICAD_CLI:-}" ] && [ -x "$HOME/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" ]; then
  export KICAD_CLI="$HOME/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
fi
$PY -c "from cvhack.tooling import kicad_cli as k; import sys; sys.exit(0 if k.available() else 1)" \
  && ok "kicad-cli found (real KiCad ERC + PDF export)" || warn "kicad-cli not found: native ERC only, no PDF export"

$PY -c "from cvhack.tooling import pcb; import sys; sys.exit(0 if pcb.can_route() else 1)" \
  && ok "3D board + autorouter (Freerouting) ready" \
  || warn "autorouter not set up: 3D board will be unrouted (run demo/setup_router.sh)"

RID="_preflight"
rm -rf "runs/$RID"
$PY -m cvhack.mocks.fake_run --run-id "$RID" --speed 50 --linger 0 >/dev/null 2>&1
$PY - "$RID" <<'PY' && ok "fake run ends green (gate fails, re-plans, passes)" || bad "fake run did not end green"
import sys
from fastapi.testclient import TestClient
from dashboard.server import app
c = TestClient(app); rid = sys.argv[1]
s = c.get(f"/api/runs/{rid}/state").json()
assert s["done"]["ok"] and s["replans"] and all(r["status"] == "pass" for r in s["rules"])
assert c.get(f"/api/runs/{rid}/schematic.svg").status_code == 200
assert c.post(f"/api/runs/{rid}/export").json()["ok"]
PY
rm -rf "runs/$RID"

[ -s demo/recordings/fake-ppg-stockout/events.jsonl ] && ok "replay fallback recording present" || bad "replay recording missing"

if [ -f .env ] && grep -q '^CRUSOE_API_KEY=.' .env; then
  ok ".env has CRUSOE_API_KEY (real run possible: demo/run_demo.sh --real)"
else
  warn "no CRUSOE_API_KEY in .env: only fake and replay modes will work"
fi

PORT=${PORT:-8000}
if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then warn "port $PORT already in use (set PORT=...)"; else ok "port $PORT free"; fi

echo
[ $fails -eq 0 ] && echo "Ready." || { echo "$fails check(s) failed."; exit 1; }
