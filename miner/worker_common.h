// Host-side line protocol shared by the CPU and CUDA workers.
//
//   job <address> <prevWork> <anchor> <target> <start> <count>
//     -> result <found 0|1> <nonce> <digest> <milliseconds> <hashes>
//   digest <address> <prevWork> <anchor> <nonce>
//     -> digest <digest>
//   quit
//
// Hex fields accept an optional 0x prefix. `start` must have its low 64 bits clear;
// `count` is decimal and at most 2^32. `nonce` in a result is start | offset.
#pragma once
#include <chrono>
#include <cstdint>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "keccak.h"

namespace hc {

inline std::vector<unsigned char> hex_bytes(std::string hex, size_t expect) {
  if (hex.rfind("0x", 0) == 0 || hex.rfind("0X", 0) == 0) hex = hex.substr(2);
  if (hex.size() > expect * 2) throw std::runtime_error("hex field too long");
  hex = std::string(expect * 2 - hex.size(), '0') + hex;
  std::vector<unsigned char> out(expect);
  for (size_t i = 0; i < expect; ++i) {
    out[i] = static_cast<unsigned char>(std::stoul(hex.substr(2 * i, 2), nullptr, 16));
  }
  return out;
}

inline std::string bytes_hex(const unsigned char* data, size_t len) {
  static const char* digits = "0123456789abcdef";
  std::string s;
  s.reserve(len * 2);
  for (size_t i = 0; i < len; ++i) {
    s.push_back(digits[data[i] >> 4]);
    s.push_back(digits[data[i] & 15]);
  }
  return s;
}

struct Batch {
  hc_u64 lanes[HC_LANES];
  hc_u64 target[4];
  hc_u64 count;
  unsigned char start[32];
};

inline hc_u64 be_word(const unsigned char* p) {
  hc_u64 w = 0;
  for (int i = 0; i < 8; ++i) w = (w << 8) | p[i];
  return w;
}

inline hc_u64 le_word(const unsigned char* p) {
  hc_u64 w = 0;
  for (int i = 7; i >= 0; --i) w = (w << 8) | p[i];
  return w;
}

// Build the padded block lanes for address || nonce || prevWork || anchor.
inline void build_lanes(const std::vector<unsigned char>& address,
                        const unsigned char nonce[32],
                        const std::vector<unsigned char>& prev,
                        const std::vector<unsigned char>& anchor, hc_u64 lanes[HC_LANES]) {
  unsigned char block[HC_RATE] = {0};
  size_t at = 0;
  for (unsigned char b : address) block[at++] = b;
  for (int i = 0; i < 32; ++i) block[at++] = nonce[i];
  for (unsigned char b : prev) block[at++] = b;
  for (unsigned char b : anchor) block[at++] = b;
  if (at != HC_MESSAGE_LEN) throw std::runtime_error("message length");
  block[HC_MESSAGE_LEN] ^= 0x01;
  block[HC_RATE - 1] ^= 0x80;
  for (int i = 0; i < HC_LANES; ++i) lanes[i] = le_word(block + 8 * i);
}

inline Batch parse_job(std::istringstream& in) {
  std::string address, prev, anchor, target, start, count;
  if (!(in >> address >> prev >> anchor >> target >> start >> count)) {
    throw std::runtime_error("job needs 6 fields");
  }
  Batch b{};
  auto a = hex_bytes(address, 20);
  auto p = hex_bytes(prev, 32);
  auto an = hex_bytes(anchor, 32);
  auto t = hex_bytes(target, 32);
  auto s = hex_bytes(start, 32);
  for (int i = 24; i < 32; ++i) {
    if (s[i] != 0) throw std::runtime_error("start must have its low 64 bits clear");
  }
  for (int i = 0; i < 32; ++i) b.start[i] = s[i];
  build_lanes(a, b.start, p, an, b.lanes);
  for (int i = 0; i < 4; ++i) b.target[i] = be_word(&t[8 * i]);
  unsigned long long c = std::stoull(count);
  if (c == 0 || c > (1ULL << 32)) throw std::runtime_error("count must be 1..2^32");
  b.count = c;
  return b;
}

inline std::string digest_hex(const hc_u64 out[4]) {
  unsigned char bytes[32];
  for (int i = 0; i < 4; ++i) {
    for (int k = 0; k < 8; ++k) bytes[8 * i + k] = static_cast<unsigned char>(out[i] >> (8 * k));
  }
  return bytes_hex(bytes, 32);
}

inline std::string nonce_hex(const Batch& b, hc_u64 offset) {
  unsigned char n[32];
  for (int i = 0; i < 24; ++i) n[i] = b.start[i];
  for (int i = 0; i < 8; ++i) n[24 + i] = static_cast<unsigned char>(offset >> (8 * (7 - i)));
  return bytes_hex(n, 32);
}

inline std::string result_line(const Batch& b, bool found, hc_u64 offset, const hc_u64 out[4],
                               double ms, hc_u64 hashes) {
  std::ostringstream o;
  o << "result " << (found ? 1 : 0) << " 0x" << (found ? nonce_hex(b, offset) : std::string(64, '0'))
    << " 0x" << (found ? digest_hex(out) : std::string(64, '0')) << " " << ms << " " << hashes;
  return o.str();
}

// digest <address> <prev> <anchor> <nonce>: hash one exact nonce through the batch path.
inline std::string digest_command(std::istringstream& in) {
  std::string address, prev, anchor, nonce;
  if (!(in >> address >> prev >> anchor >> nonce)) throw std::runtime_error("digest needs 4 fields");
  auto a = hex_bytes(address, 20);
  auto p = hex_bytes(prev, 32);
  auto an = hex_bytes(anchor, 32);
  auto n = hex_bytes(nonce, 32);
  unsigned char base_nonce[32];
  for (int i = 0; i < 32; ++i) base_nonce[i] = i < 24 ? n[i] : 0;
  hc_u64 lanes[HC_LANES];
  build_lanes(a, base_nonce, p, an, lanes);
  hc_u64 out[4];
  hc_hash_offset(lanes, be_word(&n[24]), out);
  return "digest 0x" + digest_hex(out);
}

inline double ms_since(std::chrono::steady_clock::time_point t0) {
  return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

}  // namespace hc
