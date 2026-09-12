// Keccak-f[1600] and the Hashcats message layout, shared by the CPU and CUDA workers.
//
// Hashcats work is keccak256(address[20] || nonce[32] || prevWork[32] || anchor[32]),
// a 116-byte message that fits one 136-byte Keccak block padded 0x01 ... 0x80.
// The worker only varies the low 64 bits of the nonce inside a batch. Those eight
// bytes sit at message offsets 44..51, which straddle lanes 5 and 6, so a batch is
// described by 17 base lanes (nonce low bits zero) plus a 64-bit offset.
#pragma once
#include <stdint.h>

#if defined(__CUDACC__)
#define HC_FN __host__ __device__ __forceinline__
#else
#define HC_FN static inline
#endif

typedef unsigned long long hc_u64;

#define HC_MESSAGE_LEN 116
#define HC_RATE 136
#define HC_LANES 17

HC_FN hc_u64 hc_rotl64(hc_u64 x, unsigned n) {
  return n == 0 ? x : ((x << n) | (x >> (64u - n)));
}

HC_FN hc_u64 hc_bswap64(hc_u64 x) {
  x = ((x & 0x00ff00ff00ff00ffULL) << 8) | ((x & 0xff00ff00ff00ff00ULL) >> 8);
  x = ((x & 0x0000ffff0000ffffULL) << 16) | ((x & 0xffff0000ffff0000ULL) >> 16);
  return (x << 32) | (x >> 32);
}

HC_FN void hc_keccakf(hc_u64 s[25]) {
  const hc_u64 rc[24] = {
      0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808aULL,
      0x8000000080008000ULL, 0x000000000000808bULL, 0x0000000080000001ULL,
      0x8000000080008081ULL, 0x8000000000008009ULL, 0x000000000000008aULL,
      0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000aULL,
      0x000000008000808bULL, 0x800000000000008bULL, 0x8000000000008089ULL,
      0x8000000000008003ULL, 0x8000000000008002ULL, 0x8000000000000080ULL,
      0x000000000000800aULL, 0x800000008000000aULL, 0x8000000080008081ULL,
      0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL};
  // rho rotation offsets indexed by lane x + 5y
  const unsigned rho[25] = {0,  1,  62, 28, 27, 36, 44, 6,  55, 20, 3,  10, 43,
                            25, 39, 41, 45, 15, 21, 8,  18, 2,  61, 56, 14};
#if defined(__CUDACC__)
#pragma unroll
#endif
  for (int round = 0; round < 24; ++round) {
    hc_u64 c[5], d[5], b[25];
#if defined(__CUDACC__)
#pragma unroll
#endif
    for (int x = 0; x < 5; ++x) {
      c[x] = s[x] ^ s[x + 5] ^ s[x + 10] ^ s[x + 15] ^ s[x + 20];
    }
#if defined(__CUDACC__)
#pragma unroll
#endif
    for (int x = 0; x < 5; ++x) {
      d[x] = c[(x + 4) % 5] ^ hc_rotl64(c[(x + 1) % 5], 1);
    }
#if defined(__CUDACC__)
#pragma unroll
#endif
    for (int i = 0; i < 25; ++i) {
      s[i] ^= d[i % 5];
    }
#if defined(__CUDACC__)
#pragma unroll
#endif
    for (int x = 0; x < 5; ++x) {
#if defined(__CUDACC__)
#pragma unroll
#endif
      for (int y = 0; y < 5; ++y) {
        b[y + 5 * ((2 * x + 3 * y) % 5)] = hc_rotl64(s[x + 5 * y], rho[x + 5 * y]);
      }
    }
#if defined(__CUDACC__)
#pragma unroll
#endif
    for (int y = 0; y < 5; ++y) {
#if defined(__CUDACC__)
#pragma unroll
#endif
      for (int x = 0; x < 5; ++x) {
        s[x + 5 * y] = b[x + 5 * y] ^ (~b[(x + 1) % 5 + 5 * y] & b[(x + 2) % 5 + 5 * y]);
      }
    }
    s[0] ^= rc[round];
  }
}

// Hash the batch message with the low 64 nonce bits set to `offset`.
// `base` holds the 17 padded block lanes built with those bits clear.
HC_FN void hc_hash_offset(const hc_u64 base[HC_LANES], hc_u64 offset, hc_u64 out[4]) {
  hc_u64 s[25];
#if defined(__CUDACC__)
#pragma unroll
#endif
  for (int i = 0; i < HC_LANES; ++i) {
    s[i] = base[i];
  }
#if defined(__CUDACC__)
#pragma unroll
#endif
  for (int i = HC_LANES; i < 25; ++i) {
    s[i] = 0;
  }
  // Big-endian offset bytes land at message bytes 44..51: the high half of
  // lane 5 and the low half of lane 6, both little-endian inside the lane.
  const hc_u64 r = hc_bswap64(offset);
  s[5] = (base[5] & 0x00000000ffffffffULL) | (r << 32);
  s[6] = (base[6] & 0xffffffff00000000ULL) | (r >> 32);
  hc_keccakf(s);
  out[0] = s[0];
  out[1] = s[1];
  out[2] = s[2];
  out[3] = s[3];
}

// Strict digest < target, both as 256-bit big-endian numbers.
// `out` are Keccak lanes (little-endian bytes); `target` are big-endian words, [0] most significant.
HC_FN bool hc_below_target(const hc_u64 out[4], const hc_u64 target[4]) {
#if defined(__CUDACC__)
#pragma unroll
#endif
  for (int i = 0; i < 4; ++i) {
    const hc_u64 w = hc_bswap64(out[i]);
    if (w < target[i]) return true;
    if (w > target[i]) return false;
  }
  return false;
}
