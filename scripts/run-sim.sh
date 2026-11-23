#!/bin/bash
# Run the whole being on a development machine.
# The face opens in a window, your keyboard is its ears, and `say` is its voice.

set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
    cat <<'EOF'
Usage: scripts/run-sim.sh [options]

  --model FILE.gguf   start llama-server with this model (or set BEING_MODEL)
  --cortex URL        use a model server that is already running
  --plain-openai      that server is not llama-server (no grammar, no cache hints)
  --fake              use scripted replies instead of a model
  --silent            print what it says instead of speaking aloud
  --no-face           do not open the face window
  -h, --help          show this

With no model options it uses llama-server if BEING_MODEL is set and
llama-server is installed, and scripted replies otherwise.
EOF
}

MODEL="${BEING_MODEL:-}"
CORTEX=""
PLAIN=""
FAKE=0
SILENT=""
FACE=1
while [ $# -gt 0 ]; do
    case "$1" in
        --model) MODEL="${2:?--model needs a file}"; shift ;;
        --cortex) CORTEX="${2:?--cortex needs a URL}"; shift ;;
        --plain-openai) PLAIN="--plain-openai" ;;
        --fake) FAKE=1 ;;
        --silent) SILENT="--silent" ;;
        --no-face) FACE=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

cargo build --quiet

LOGS="out/logs"
mkdir -p "$LOGS"
RUN="$(mktemp -d /tmp/being-sim.XXXXXX)"
export BEING_BUS="$RUN/bus.sock"
export BEING_MIND="${BEING_MIND:-$HOME/.local/share/being/mind}"
PORT="${BEING_PORT:-8377}"
mkdir -p "$BEING_MIND"

PIDS=()
cleanup() {
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    rm -rf "$RUN"
}
trap cleanup EXIT

# Without a face window, the brainstem must not wait for one.
REQUIRE="face,mind,voice"
if [ "$FACE" = 0 ]; then
    REQUIRE="mind,voice"
fi
target/debug/brainstem --require "$REQUIRE" 2>"$LOGS/brainstem.log" &
PIDS+=($!)

if [ -n "$CORTEX" ]; then
    echo "Cortex: $CORTEX"
elif [ "$FAKE" = 0 ] && [ -n "$MODEL" ] && command -v llama-server >/dev/null; then
    echo "Cortex: llama-server with $(basename "$MODEL") (loading; log in $LOGS/cortex.log)"
    llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" -c 4096 \
        --reasoning-budget 0 --slot-save-path "$BEING_MIND" >"$LOGS/cortex.log" 2>&1 &
    PIDS+=($!)
    CORTEX="http://127.0.0.1:$PORT"
else
    if [ "$FAKE" = 0 ]; then
        echo "No model server found, so replies are scripted. See docs/manual/DEVELOPER-GUIDE.md, section 2, \"Three ways to run Pip\"."
    fi
    echo "Cortex: scripted replies"
    python3 sim/fake_cortex.py --port "$PORT" >"$LOGS/cortex.log" 2>&1 &
    PIDS+=($!)
    CORTEX="http://127.0.0.1:$PORT"
fi

python3 -m mind --cortex "$CORTEX" $PLAIN 2>"$LOGS/mind.log" &
PIDS+=($!)

if [ "$FACE" = 1 ]; then
    target/debug/face --size 960x600 2>"$LOGS/face.log" &
    PIDS+=($!)
fi

echo "Memory: $BEING_MIND"
echo
python3 sim/body.py $SILENT
