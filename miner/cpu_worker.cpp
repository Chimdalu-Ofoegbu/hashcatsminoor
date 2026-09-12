// Single-thread CPU worker. Same protocol and same Keccak core as the CUDA worker.
// Use it to validate the protocol end to end and to test the coordinator without a GPU.
#include <iostream>
#include <string>

#include "worker_common.h"

int main() {
  std::ios::sync_with_stdio(false);
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
      auto t0 = std::chrono::steady_clock::now();
      hc_u64 out[4] = {0, 0, 0, 0};
      bool found = false;
      hc_u64 offset = 0;
      for (hc_u64 o = 0; o < b.count; ++o) {
        hc_hash_offset(b.lanes, o, out);
        if (hc_below_target(out, b.target)) {
          found = true;
          offset = o;
          break;
        }
      }
      hc_u64 hashes = found ? offset + 1 : b.count;
      std::cout << hc::result_line(b, found, offset, out, hc::ms_since(t0), hashes) << std::endl;
    } catch (const std::exception& e) {
      std::cout << "error " << e.what() << std::endl;
    }
  }
  return 0;
}
