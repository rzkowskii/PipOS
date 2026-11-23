#!/bin/sh
# Build only the dedicated OpenJev library. The regular cortex keeps upstream llama.cpp.
set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: sh lab/build_openjev_bridge.sh LLAMA_CPP_B12359 NEW_OUTPUT_DIR native|ivybridge" >&2
    exit 2
fi

recipe_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
source_dir=$(CDPATH='' cd -- "$1" && pwd)
output_dir=$2
mode=$3
if [ -e "$output_dir" ]; then
    echo "choose a new output directory" >&2
    exit 2
fi

case "$mode" in
    native)
        set -- -DGGML_NATIVE=ON
        ;;
    ivybridge)
        set -- -DGGML_NATIVE=OFF -DGGML_SSE42=ON -DGGML_AVX=ON -DGGML_F16C=ON \
            -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_BMI2=OFF -DGGML_AVX512=OFF -DGGML_AVX_VNNI=OFF
        ;;
    *)
        echo "choose native or ivybridge" >&2
        exit 2
        ;;
esac

mkdir -p "$output_dir"
output_dir=$(CDPATH='' cd -- "$output_dir" && pwd)
local_source="$output_dir/llama.cpp"
cp -R "$source_dir" "$local_source"
patch -d "$local_source" -p1 < "$recipe_dir/openjev-llama-output.patch"

cmake -S "$local_source" -B "$output_dir/build" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=ON -DLLAMA_BUILD_COMMON=OFF -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF -DLLAMA_BUILD_SERVER=OFF \
    -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DGGML_METAL=OFF -DGGML_BLAS=OFF \
    -DGGML_VULKAN=OFF -DGGML_OPENMP=OFF "$@"
cmake --build "$output_dir/build" --target llama --parallel 4

case "$(uname -s)" in
    Darwin) suffix=dylib; shared=-dynamiclib ;;
    Linux) suffix=so; shared=-shared ;;
    *) echo "bridge build supports Darwin and Linux" >&2; exit 2 ;;
esac

lib_dir="$output_dir/build/bin"
"${CXX:-c++}" -std=c++17 -O2 -fPIC "$shared" \
    -I"$local_source/include" -I"$local_source/ggml/include" "$recipe_dir/openjev_bridge.cpp" \
    -L"$lib_dir" -Wl,-rpath,"$lib_dir" -lllama -o "$output_dir/libopenjev.$suffix"
echo "$output_dir/libopenjev.$suffix"
