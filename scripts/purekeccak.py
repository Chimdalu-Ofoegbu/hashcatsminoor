"""Pure-Python Keccak-256 (Ethereum flavour, 0x01 padding). Standard library only,
so the GPU host can verify the worker without installing anything."""

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_RHO = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14]
_MASK = (1 << 64) - 1


def _rotl(x, n):
    return ((x << n) | (x >> (64 - n))) & _MASK if n else x


def keccak_f(s):
    for rc in _RC:
        c = [s[x] ^ s[x + 5] ^ s[x + 10] ^ s[x + 15] ^ s[x + 20] for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        s = [s[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(s[x + 5 * y], _RHO[x + 5 * y])
        s = [b[i] ^ ((~b[(i % 5 + 1) % 5 + 5 * (i // 5)]) & _MASK & b[(i % 5 + 2) % 5 + 5 * (i // 5)]) for i in range(25)]
        s[0] ^= rc
    return s


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate:
        padded.append(0)
    padded[-1] ^= 0x80
    s = [0] * 25
    for off in range(0, len(padded), rate):
        for i in range(rate // 8):
            s[i] ^= int.from_bytes(padded[off + 8 * i: off + 8 * i + 8], "little")
        s = keccak_f(s)
    return b"".join(s[i].to_bytes(8, "little") for i in range(4))


def hashcats_digest(address: bytes, nonce: int, prev: int, anchor: int) -> bytes:
    return keccak256(address + nonce.to_bytes(32, "big") + prev.to_bytes(32, "big") + anchor.to_bytes(32, "big"))
