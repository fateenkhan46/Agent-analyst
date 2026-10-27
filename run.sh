#!/usr/bin/env bash
# Start Agent Analyst on this machine.
#
#   ./run.sh
#
# Everything runs locally: a Python API on :8000, a Next.js UI on :3000, and a
# SQLite file plus an uploads folder under ./data. No data leaves this machine
# unless you set ANTHROPIC_API_KEY, which this script deliberately does not.
#
#   ./run.sh --sample   also loads the worked sample project
#   ./run.sh --stop     stops both services
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
RUN_DIR="$ROOT/.run"; mkdir -p "$RUN_DIR"
API_PORT="${API_PORT:-8000}"; WEB_PORT="${WEB_PORT:-3000}"

c()   { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
info(){ c "0;36" "  $1"; }
ok()  { c "0;32" "  ✓ $1"; }
warn(){ c "0;33" "  ! $1"; }
die() { c "0;31" "  ✗ $1"; exit 1; }

stop_all() {
  for name in api web; do
    local pf="$RUN_DIR/$name.pid"
    if [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null; then
      kill "$(cat "$pf")" 2>/dev/null || true
      ok "stopped $name"
    fi
    rm -f "$pf"
  done
}

if [[ "${1:-}" == "--stop" ]]; then stop_all; exit 0; fi
trap 'echo; info "shutting down"; stop_all' INT TERM

echo
c "1;37" "Agent Analyst — local"
echo

# ---------------------------------------------------------------- checks
command -v python3 >/dev/null || die "python3 not found. Install Python 3.11 or newer."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' \
  || die "Python $PYV found; 3.11 or newer is required."
ok "python $PYV"

command -v node >/dev/null || die "node not found. Install Node 18 or newer from nodejs.org."
NODEV=$(node -p 'process.versions.node.split(".")[0]')
(( NODEV >= 18 )) || die "node $NODEV found; 18 or newer is required."
ok "node $(node -p 'process.versions.node')"

for p in "$API_PORT" "$WEB_PORT"; do
  if (command -v lsof >/dev/null && lsof -iTCP:"$p" -sTCP:LISTEN -t >/dev/null 2>&1); then
    die "port $p is already in use. Run ./run.sh --stop, or set API_PORT/WEB_PORT."
  fi
done

# ---------------------------------------------------------------- backend
if [[ ! -d "$ROOT/.venv" ]]; then
  info "creating the Python environment (one time, a minute or two)"
  python3 -m venv "$ROOT/.venv"
fi
PY="$ROOT/.venv/bin/python"
"$PY" -m pip install --quiet --upgrade pip
if ! "$PY" -c 'import fastapi, pandas, duckdb, openpyxl' 2>/dev/null; then
  info "installing Python dependencies"
  "$PY" -m pip install --quiet -r "$ROOT/backend/requirements.txt"
fi
ok "python dependencies ready"

# ---------------------------------------------------------------- sample data
if [[ ! -f "$ROOT/samples/sample_project/project_costs.xlsx" ]]; then
  info "generating the sample data"
  (cd "$ROOT" && "$PY" samples/generate.py >/dev/null)
fi
ok "sample data present"

# ---------------------------------------------------------------- frontend
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  info "installing UI dependencies (one time)"
  (cd "$ROOT/frontend" && npm install --silent)
fi
if [[ ! -d "$ROOT/frontend/.next" ]]; then
  info "building the UI (one time, about a minute)"
  (cd "$ROOT/frontend" && npm run build >/dev/null 2>&1) || die "the UI build failed"
fi
ok "ui ready"

# ---------------------------------------------------------------- start
mkdir -p "$ROOT/data"
info "starting the API on :$API_PORT"
(cd "$ROOT/backend" && DATA_DIR="$ROOT/data" ENVIRONMENT=development \
   "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" \
   --log-level warning > "$RUN_DIR/api.log" 2>&1 & echo $! > "$RUN_DIR/api.pid")

for i in $(seq 1 40); do
  curl -fsS --noproxy '*' "http://127.0.0.1:$API_PORT/api/v1/system/health" >/dev/null 2>&1 && break
  sleep 0.5
  (( i == 40 )) && { tail -20 "$RUN_DIR/api.log"; die "the API did not start"; }
done
ok "API healthy"

info "starting the UI on :$WEB_PORT"
(cd "$ROOT/frontend" && BACKEND_URL="http://127.0.0.1:$API_PORT" \
   npx next start -p "$WEB_PORT" > "$RUN_DIR/web.log" 2>&1 & echo $! > "$RUN_DIR/web.pid")

for i in $(seq 1 40); do
  curl -fsS --noproxy '*' "http://127.0.0.1:$WEB_PORT/" >/dev/null 2>&1 && break
  sleep 0.5
  (( i == 40 )) && { tail -20 "$RUN_DIR/web.log"; die "the UI did not start"; }
done
ok "UI healthy"

if [[ "${1:-}" == "--sample" ]]; then
  info "loading the sample project"
  (cd "$ROOT" && "$PY" scripts/seed_sample_project.py \
     --base "http://127.0.0.1:$API_PORT" --auto-approve | sed 's/^/    /')
fi

echo
c "1;32" "  Open  http://localhost:$WEB_PORT"
echo
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  warn "ANTHROPIC_API_KEY is set in your shell, so uploaded data and transcripts"
  warn "WILL be sent to a third-party API. Unset it to keep everything local."
else
  ok "no LLM key set — nothing leaves this machine"
fi
echo "  logs: $RUN_DIR/api.log, $RUN_DIR/web.log"
echo "  stop: ./run.sh --stop   (or press Ctrl-C)"
echo

while true; do
  sleep 5
  kill -0 "$(cat "$RUN_DIR/api.pid")" 2>/dev/null || { warn "the API stopped"; tail -20 "$RUN_DIR/api.log"; break; }
  kill -0 "$(cat "$RUN_DIR/web.pid")" 2>/dev/null || { warn "the UI stopped";  tail -20 "$RUN_DIR/web.log";  break; }
done
stop_all
