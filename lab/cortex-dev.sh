#!/bin/bash
# A real llama-server at full speed, for developing against on the Mac.
#
# It runs in the builder container (so nothing is installed on macOS), built
# from the same llama.cpp source the image uses, and serves the model in
# out/models/ on http://127.0.0.1:8377.
#
#   lab/cortex-dev.sh start     build it if needed, then run it in the background
#   lab/cortex-dev.sh stop
#   lab/cortex-dev.sh logs
#
# Then, for example:  scripts/run-sim.sh --cortex http://127.0.0.1:8377

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
NAME="beingos-cortex-dev"
PORT="${BEING_PORT:-8377}"
VERSION="v1.0.0"

docker_() { docker --context colima-beingos "$@"; }

case "${1:-start}" in
    start)
        [ -f out/models/reflex.gguf ] || { echo "no model: run lab/build.sh model FILE.gguf first" >&2; exit 1; }
        docker_ rm -f "$NAME" >/dev/null 2>&1 || true
        docker_ run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" \
            -v beingos-work:/work -v "$ROOT:/src:ro" beingos-builder bash -euo pipefail -c '
            if [ ! -x /work/cortex-dev/bin/llama-server ]; then
                rm -rf /work/cortex-dev-src /work/cortex-dev
                mkdir -p /work/cortex-dev-src
                tar -xzf /work/dl/cortex/'"$VERSION"'.tar.gz -C /work/cortex-dev-src --strip-components=1
                cmake -S /work/cortex-dev-src -B /work/cortex-dev \
                    -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
                    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_CURL=OFF \
                    -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DGGML_OPENMP=OFF
                cmake --build /work/cortex-dev --target llama-server -j"$(nproc)"
            fi
            mkdir -p /tmp/slots
            exec /work/cortex-dev/bin/llama-server --model /src/out/models/reflex.gguf \
                --host 0.0.0.0 --port 8080 --ctx-size 4096 --parallel 1 --threads 4 \
                --reasoning-budget 0 --slot-save-path /tmp/slots --no-webui
        ' >/dev/null
        printf 'Starting'
        for _ in $(seq 1 600); do
            if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
                echo " ready on http://127.0.0.1:$PORT"
                exit 0
            fi
            if [ "$(docker_ inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
                echo " failed:" >&2
                docker_ logs --tail 20 "$NAME" >&2
                exit 1
            fi
            printf '.'
            sleep 2
        done
        echo " timed out" >&2
        exit 1
        ;;
    stop)
        docker_ rm -f "$NAME" >/dev/null 2>&1 || true
        ;;
    logs)
        docker_ logs --tail "${2:-40}" "$NAME"
        ;;
    *)
        echo "usage: lab/cortex-dev.sh start|stop|logs" >&2
        exit 2
        ;;
esac
