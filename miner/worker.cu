// CUDA worker: one process per GPU, same line protocol as cpu_worker.
//
// Environment:
//   CUDA_DEVICE   device index (default 0)
//   CUDA_THREADS  threads per block (default 256)
//   CUDA_BLOCKS   blocks per launch (default 8 x multiprocessor count)
//
// Not compiled in the environment this was written in: validate with
// scripts/gpu_selftest.py on the GPU host before mining with it.
#include <cuda_runtime.h>

#include <cstdlib>
#include <iostream>
#include <string>

#include "worker_common.h"

struct DeviceResult {
  unsigned int found;
  hc_u64 offset;
  hc_u64 digest[4];
};

__global__ void hc_search(const hc::Batch* job, DeviceResult* res) {
  hc_u64 base[HC_LANES];
  hc_u64 target[4];
#pragma unroll
  for (int i = 0; i < HC_LANES; ++i) base[i] = job->lanes[i];
#pragma unroll
  for (int i = 0; i < 4; ++i) target[i] = job->target[i];
  const hc_u64 count = job->count;
  const hc_u64 stride = static_cast<hc_u64>(gridDim.x) * blockDim.x;
  unsigned int iter = 0;
  for (hc_u64 o = static_cast<hc_u64>(blockIdx.x) * blockDim.x + threadIdx.x; o < count; o += stride) {
    if ((++iter & 31u) == 0u && *reinterpret_cast<volatile unsigned int*>(&res->found)) return;
    hc_u64 out[4];
    hc_hash_offset(base, o, out);
    if (hc_below_target(out, target)) {
      if (atomicCAS(&res->found, 0u, 1u) == 0u) {
        res->offset = o;
        res->digest[0] = out[0];
        res->digest[1] = out[1];
        res->digest[2] = out[2];
        res->digest[3] = out[3];
      }
      return;
    }
  }
}

static void check(cudaError_t err, const char* what) {
  if (err != cudaSuccess) {
    throw std::runtime_error(std::string(what) + ": " + cudaGetErrorString(err));
  }
}

static int env_int(const char* name, int fallback) {
  const char* v = std::getenv(name);
  return v && *v ? std::atoi(v) : fallback;
}

int main() {
  std::ios::sync_with_stdio(false);
  try {
    const int device = env_int("CUDA_DEVICE", 0);
    check(cudaSetDevice(device), "cudaSetDevice");
    cudaDeviceProp props{};
    check(cudaGetDeviceProperties(&props, device), "cudaGetDeviceProperties");
    const int threads = env_int("CUDA_THREADS", 256);
    const int blocks = env_int("CUDA_BLOCKS", props.multiProcessorCount * 8);
    if (threads < 32 || threads > 1024 || threads % 32 != 0 || blocks < 1) {
      throw std::runtime_error("bad CUDA_THREADS or CUDA_BLOCKS");
    }
    std::cerr << "worker device " << device << " " << props.name << " sm" << props.major << props.minor
              << " blocks " << blocks << " threads " << threads << std::endl;

    hc::Batch* d_job = nullptr;
    DeviceResult* d_res = nullptr;
    check(cudaMalloc(&d_job, sizeof(hc::Batch)), "cudaMalloc job");
    check(cudaMalloc(&d_res, sizeof(DeviceResult)), "cudaMalloc result");

    std::string line;
    while (std::getline(std::cin, line)) {
      std::istringstream in(line);
      std::string cmd;
      if (!(in >> cmd)) continue;
      try {
        if (cmd == "quit") break;
        if (cmd == "digest") {
          std::cout << hc::digest_command(in) << std::endl;
          continue;
        }
        if (cmd != "job") throw std::runtime_error("unknown command");
        hc::Batch b = hc::parse_job(in);
        DeviceResult zero{};
        auto t0 = std::chrono::steady_clock::now();
        check(cudaMemcpy(d_job, &b, sizeof(b), cudaMemcpyHostToDevice), "copy job");
        check(cudaMemcpy(d_res, &zero, sizeof(zero), cudaMemcpyHostToDevice), "clear result");
        hc_search<<<blocks, threads>>>(d_job, d_res);
        check(cudaGetLastError(), "launch");
        check(cudaDeviceSynchronize(), "sync");
        DeviceResult r{};
        check(cudaMemcpy(&r, d_res, sizeof(r), cudaMemcpyDeviceToHost), "copy result");
        const double ms = hc::ms_since(t0);
        // Without a hit every offset was tried; with one, the count is approximate.
        const hc_u64 hashes = r.found ? r.offset + 1 : b.count;
        std::cout << hc::result_line(b, r.found != 0, r.offset, r.digest, ms, hashes) << std::endl;
      } catch (const std::exception& e) {
        std::cout << "error " << e.what() << std::endl;
      }
    }
    cudaFree(d_job);
    cudaFree(d_res);
  } catch (const std::exception& e) {
    std::cerr << "fatal " << e.what() << std::endl;
    return 1;
  }
  return 0;
}
