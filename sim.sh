#!/usr/bin/env bash
# One entry point for the simulation and the 3D viewer.
#   ./sim.sh            run the sim and watch it (rule sellers, no API key needed)
#   ./sim.sh llm        same, with LLM sellers (needs ANTHROPIC_API_KEY or OPENAI_API_KEY)
#   ./sim.sh help       everything else
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

acs() { .venv/bin/acs "$@"; }

viewer_up() { curl -fsS -o /dev/null --max-time 1 "${URL}api/runs" 2>/dev/null; }

start_viewer() {
  mkdir -p "$RUNS"
  if viewer_up; then
    say "viewer already running at ${URL}"
  else
    say "starting viewer at ${URL} (log: ${LOG_FILE})"
    nohup .venv/bin/acs serve --runs "$RUNS" --port "$PORT" >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    for _ in $(seq 1 50); do viewer_up && break; sleep 0.1; done
    viewer_up || die "viewer did not start, see ${LOG_FILE}"
  fi
  [[ "${ACS_NO_BROWSER:-}" == "1" ]] || open_browser
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

need_key() {
  local model="${ACS_SELLER_MODEL:-anthropic:claude-haiku-5-5}"
  case "$model" in
    anthropic:*) [[ -n "${ANTHROPIC_API_KEY:-}" ]] || die "export ANTHROPIC_API_KEY=... first (model: $model)" ;;
    openai:*)    [[ -n "${OPENAI_API_KEY:-}" ]]    || die "export OPENAI_API_KEY=... first (model: $model)" ;;
  esac
}

usage() {
  cat <<EOF
Usage: ./sim.sh [command] [options]

  (none) | run    start the viewer, open the browser, run the sim (rule sellers, no key)
  llm             same with LLM sellers (export ANTHROPIC_API_KEY, or set
                  ACS_SELLER_MODEL=openai:gpt-5-mini and export OPENAI_API_KEY)
  s1              same with System 1 buyers (needs Ollama + tev1:4b) and LLM sellers
  view            only start the viewer and open the browser
  stop            stop the background viewer
  runs            list recorded runs
  replay <dir>    re-run a recorded run without model calls and verify it
  test            run the test suite
  setup           install / update dependencies

Options after run/llm/s1 go to the simulation, e.g.
  ./sim.sh run --ticks 50 --seed 7 --no-shocks
  ./sim.sh llm --no-positioning

Env: ACS_VIEWER_PORT (8765), ACS_RUNS_DIR (runs), ACS_NO_BROWSER=1
EOF
}

cmd="${1:-run}"
[[ $# -gt 0 ]] && shift
case "$cmd" in
  run)    ensure_env; start_viewer; acs run --out "$RUNS" "$@" ;;
  llm)    ensure_env; need_key; start_viewer; acs run --out "$RUNS" --sellers llm "$@" ;;
  s1)     ensure_env; need_key; start_viewer; acs run --out "$RUNS" --buyers s1 --sellers llm "$@" ;;
  view)   ensure_env; start_viewer ;;
  stop)   stop_viewer ;;
  runs)   ls -1t "$RUNS" 2>/dev/null | grep -v '^\.' || say "no runs yet" ;;
  replay) ensure_env; [[ $# -ge 1 ]] || die "usage: ./sim.sh replay runs/<run-folder>"; acs replay "$@" ;;
  test)   ensure_env; .venv/bin/pytest -q "$@" ;;
  setup)  command -v uv >/dev/null || die "uv is not installed"; uv sync --extra dev ;;
  help|-h|--help) usage ;;
  *)      usage; exit 1 ;;
esac

if [[ "$cmd" =~ ^(run|llm|s1)$ ]]; then
  say "run saved; the viewer keeps running at ${URL} (./sim.sh stop to stop it)"
fi
