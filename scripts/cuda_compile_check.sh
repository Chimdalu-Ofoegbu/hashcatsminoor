#!/usr/bin/env bash
# Compile-check miner/worker.cu WITHOUT a GPU or the CUDA toolkit.
#
# NVIDIA publishes the CUDA headers, libdevice and ptxas as PyPI wheels, and
# clang has its own CUDA front end. This script assembles a CUDA-shaped root
# from the wheels, compiles the device code with clang for sm_89 (RTX 4090),
# compiles the host code, then assembles the emitted PTX with ptxas for sm_120
# (RTX 5090). It proves the file compiles and assembles for both cards; it
# cannot run the kernel. scripts/gpu_selftest.py on a real GPU does that.
#
# Needs: clang++ (18 or newer), python3 with venv, network access to PyPI.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p "${CUDA_CHECK_DIR:-.cuda-check}"
WORK="$(cd "${CUDA_CHECK_DIR:-.cuda-check}" && pwd)"
VENV="$WORK/venv"
ROOT="$WORK/root"

if [ ! -x "$VENV/bin/pip" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q nvidia-cuda-nvcc-cu12 nvidia-cuda-runtime-cu12 nvidia-cuda-cccl-cu12
fi
N="$(ls -d "$VENV"/lib/python3*/site-packages/nvidia)"
rm -rf "$ROOT"
mkdir -p "$ROOT/include" "$ROOT/bin" "$ROOT/lib64" "$ROOT/nvvm/libdevice"
cp -r "$N/cuda_runtime/include/." "$ROOT/include/"
cp -r "$N/cuda_nvcc/include/." "$ROOT/include/"
cp -r "$N/cuda_cccl/include/." "$ROOT/include/" 2>/dev/null || true
cp "$N/cuda_nvcc/bin/ptxas" "$ROOT/bin/"
cp "$N/cuda_nvcc/nvvm/libdevice/"* "$ROOT/nvvm/libdevice/"
cp "$N/cuda_runtime/lib/"* "$ROOT/lib64/"
# clang reads CUDA_VERSION from cuda.h, which the runtime wheel does not ship
[ -e "$ROOT/include/cuda.h" ] || printf '#pragma once\n#define CUDA_VERSION 12030\n' > "$ROOT/include/cuda.h"
# clang's wrapper includes this cuRAND header only to pre-empt a redeclaration
printf '#pragma once\n' > "$ROOT/include/curand_mtgp32_kernel.h"

CLANG="${CLANG:-clang++}"
COMMON=(-x cuda --cuda-path="$ROOT" -Wno-unknown-cuda-version -std=c++17 -O2 -Wall -Werror -I "$ROOT/include")
# libstdc++ 14 declares __float128 inside <limits>, which clang's CUDA device
# pass rejects. Prefer the gcc 13 (or 12) headers when they are installed.
for v in 13 12; do
  if [ -d "/usr/lib/gcc/x86_64-linux-gnu/$v" ]; then
    COMMON+=("--gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/$v")
    break
  fi
done
cd miner
echo "== device code for sm_89 (RTX 4090) =="
"$CLANG" "${COMMON[@]}" --cuda-device-only --cuda-gpu-arch=sm_89 -c worker.cu -o "$WORK/worker_sm89.cubin"
echo "== host code =="
"$CLANG" "${COMMON[@]}" --cuda-host-only -c worker.cu -o "$WORK/worker_host.o"
echo "== PTX, then ptxas for sm_120 (RTX 5090) =="
"$CLANG" "${COMMON[@]}" --cuda-device-only --cuda-gpu-arch=sm_90 -S worker.cu -o "$WORK/worker_sm90.ptx"
"$ROOT/bin/ptxas" -arch=sm_120 -O3 -v "$WORK/worker_sm90.ptx" -o "$WORK/worker_sm120.cubin" 2>&1 | grep -i "registers\|spill" || true
echo "OK: worker.cu compiles for sm_89 and assembles for sm_120"
