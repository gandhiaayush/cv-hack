#!/usr/bin/env bash
# Start the dashboard and a run.
#   demo/run_demo.sh            # fake run (no agents needed)
#   demo/run_demo.sh --real     # real agents (A's smoke_test.py mother, bridged from runs/run.db)
#   demo/run_demo.sh --replay runs/<good-run>   # replay a recorded run as a fallback
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
[ -x "$PY" ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
if [ ! -d dashboard/web/dist ]; then (cd dashboard/web && npm install --no-audit --no-fund && npm run build); fi
if [ -z "${KICAD_CLI:-}" ] && [ -x "$HOME/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" ]; then
  export KICAD_CLI="$HOME/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
fi

PORT=${PORT:-8000}
.venv/bin/uvicorn dashboard.server:app --port "$PORT" --log-level warning &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
sleep 1
echo "Dashboard: http://localhost:$PORT"
(command -v open >/dev/null && open "http://localhost:$PORT") || true

RUN_ID="demo-$(date +%H%M%S)"
case "${1:-}" in
  --real)
    # A's agents log to runs/run.db; the bridge turns that into dashboard events.
    # Override the entrypoint with REAL_CMD="..." once A has a proper one.
    $PY -m cvhack.bridge &
    BRIDGE=$!
    trap 'kill $SERVER $BRIDGE 2>/dev/null' EXIT
    ${REAL_CMD:-$PY smoke_test.py mother} "${@:2}"
    sleep 2   # let the bridge flush the last rows
    ;;
  --replay)
    SRC="${2:?usage: --replay runs/<run_id>}"
    $PY -m demo.replay "$SRC" --run-id "$RUN_ID"
    ;;
  *)
    $PY -m cvhack.mocks.fake_run --run-id "$RUN_ID" --speed "${SPEED:-1}" --linger 1800
    ;;
esac
wait
