#!/usr/bin/env bash
# One entry point for the simulation and the 3D viewer.
#   ./sim.sh                                  start the viewer
#   ./sim.sh run                              run the sim (rule sellers, rule buyers)
#   ./sim.sh run --sellers llm --buyers s1    choose the decision models
#   ./sim.sh help                             everything else
set -euo pipefail

cd "$(dirname "$0")"
PORT="${ACS_VIEWER_PORT:-8765}"
RUNS="${ACS_RUNS_DIR:-runs}"
URL="http://127.0.0.1:${PORT}/"
PID_FILE="${RUNS}/.viewer.pid"
LOG_FILE="${RUNS}/.viewer.log"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

ensure_env() {
  command -v uv >/dev/null || die "uv is not installed: https://docs.astral.sh/uv/getting-started/installation/"
  if [[ ! -x .venv/bin/acs ]] || [[ pyproject.toml -nt .venv/bin/acs ]] || [[ uv.lock -nt .venv/bin/acs ]]; then
    say "installing dependencies (first run or dependencies changed)"
    uv sync --extra dev -q
    touch .venv/bin/acs
  fi
}

viewer_up() { curl -fsS -o /dev/null --max-time 1 "${URL}api/runs" 2>/dev/null; }

# start_viewer [open]: start the viewer if needed; open the browser only when asked
start_viewer() {
  mkdir -p "$RUNS"
  if viewer_up; then
    say "viewer running at ${URL}"
  else
    say "starting viewer at ${URL} (log: ${LOG_FILE})"
    nohup .venv/bin/acs serve --runs "$RUNS" --port "$PORT" >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    for _ in $(seq 1 50); do viewer_up && break; sleep 0.1; done
    viewer_up || die "viewer did not start, see ${LOG_FILE}"
  fi
  if [[ "${1:-}" == "open" && "${ACS_NO_BROWSER:-}" != "1" ]]; then open_browser; fi
}

open_browser() {
  if command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 &
  elif command -v open >/dev/null; then open "$URL"
  else say "open ${URL} in your browser"; fi
}

stop_viewer() {
  if [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" 2>/dev/null; then
    say "viewer stopped"
  else
    pkill -f "acs serve --runs ${RUNS} --port ${PORT}" && say "viewer stopped" || say "viewer was not running"
  fi
  rm -f "$PID_FILE"
}

# fail early when the run asks for LLM sellers but the provider key is not exported
need_key() {
  local prev="" llm=0 a
  for a in "$@"; do
    if [[ "$a" == "--sellers=llm" || ( "$prev" == "--sellers" && "$a" == "llm" ) ]]; then llm=1; fi
    prev="$a"
  done
  [[ $llm == 1 ]] || return 0
  local model="${ACS_SELLER_MODEL:-anthropic:claude-haiku-5-5}"
  case "$model" in
    anthropic:*) [[ -n "${ANTHROPIC_API_KEY:-}" ]] || die "export ANTHROPIC_API_KEY=... first (model: $model)" ;;
    openai:*)    [[ -n "${OPENAI_API_KEY:-}" ]]    || die "export OPENAI_API_KEY=... first (model: $model)" ;;
  esac
}

usage() {
  cat <<'EOF'
Usage: ./sim.sh [command] [options]

  (none) | view   start the 3D viewer in the background and open it in the browser
  run [options]   run the simulation (starts the viewer too if it is not running)
  stop            stop the background viewer
  runs            list recorded runs, newest first
  replay <dir>    re-run a recorded run without model calls and verify it
  test            run the test suite
  setup           install / update dependencies

Run options:
  --sellers rule|llm   rule (default), or a hosted LLM: Claude Haiku with ANTHROPIC_API_KEY,
                       or ACS_SELLER_MODEL=openai:gpt-5-mini with OPENAI_API_KEY
  --buyers rule|s1     rule (default), or the local System 1 decision model (Ollama + tev1:4b)
  --ticks N  --seed N  --no-shocks  --no-positioning   (all: .venv/bin/acs run --help)

Examples:
  ./sim.sh                                 # open the viewer
  ./sim.sh run                             # rule sellers, rule buyers
  ./sim.sh run --sellers llm               # Haiku sellers, rule buyers
  ./sim.sh run --sellers llm --buyers s1   # full setup (GPU workstation)

Env: ACS_VIEWER_PORT (8765), ACS_RUNS_DIR (runs), ACS_NO_BROWSER=1
EOF
}

cmd="${1:-view}"
[[ $# -gt 0 ]] && shift
case "$cmd" in
  view)   ensure_env; start_viewer open ;;
  run)    ensure_env; need_key "$@"; start_viewer
          .venv/bin/acs run --out "$RUNS" "$@"
          say "watch it at ${URL} (./sim.sh to open the viewer, ./sim.sh stop to stop it)" ;;
  stop)   stop_viewer ;;
  runs)   ls -1t "$RUNS" 2>/dev/null | grep -v '^\.' || say "no runs yet" ;;
  replay) ensure_env; [[ $# -ge 1 ]] || die "usage: ./sim.sh replay runs/<run-folder>"; .venv/bin/acs replay "$@" ;;
  test)   ensure_env; .venv/bin/pytest -q "$@" ;;
  setup)  command -v uv >/dev/null || die "uv is not installed"; uv sync --extra dev ;;
  help|-h|--help) usage ;;
  *)      usage; exit 1 ;;
esac
