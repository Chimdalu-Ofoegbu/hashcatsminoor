#!/usr/bin/env bash
# Run on the rented GPU host, from the repo root, after cloning it there:
#   git clone <this repo> ~/hashcatsminoor && cd ~/hashcatsminoor && bash scripts/remote_setup.sh
# Builds the CUDA worker for the installed GPU, then validates and benchmarks it.
# Needs: nvidia-smi, nvcc (a CUDA *devel* image, 12.8 or newer for RTX 5090), g++, python3.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null || ! command -v g++ >/dev/null || ! command -v make >/dev/null; then
  echo "== installing python3, g++, make =="
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq python3 build-essential >/dev/null
fi

echo "== GPUs =="
nvidia-smi --query-gpu=index,name,compute_cap,memory.total --format=csv || { echo "nvidia-smi failed: no NVIDIA driver here"; exit 1; }
command -v nvcc >/dev/null || { echo "nvcc not found: use a CUDA devel image or install the toolkit"; exit 1; }
nvcc --version | tail -n 2

ARCH="${ARCH:-$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -n1 | tr -d '. ')}"
echo "== building miner/worker for sm_${ARCH} =="
make -C miner worker ARCH="${ARCH}"
make -C miner cpu_worker

SECONDS_BENCH="${BENCH_SECONDS:-10}"
echo "== self-test and ${SECONDS_BENCH}s benchmark on every GPU =="
python3 scripts/gpu_selftest.py --seconds "${SECONDS_BENCH}"
echo "== done: start the coordinator on your own machine with SSH_TARGET pointing here =="
