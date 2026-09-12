"""Hashcats proof-of-work reference, pure Python.

A cat is found by a Keccak-256 digest below the miner's current target:

    keccak256(address[20] || nonce[32] || prevWork[32] || anchor[32]) < target

Every field is big-endian. This is Ethereum Keccak, not NIST SHA3-256. The
module is the ground truth the C++ and CUDA workers are checked against.
"""
from __future__ import annotations

import os

from eth_utils import keccak

MESSAGE_LEN = 116
KECCAK_RATE = 136


def _bytes32(value: int | bytes | str) -> bytes:
    if isinstance(value, bytes):
        if len(value) != 32:
            raise ValueError("expected 32 bytes")
        return value
    if isinstance(value, str):
        value = int(value, 16)
    if not 0 <= value < 1 << 256:
        raise ValueError("value does not fit in 256 bits")
    return value.to_bytes(32, "big")


def address_bytes(address: str) -> bytes:
    raw = bytes.fromhex(address.removeprefix("0x"))
    if len(raw) != 20:
        raise ValueError("address must be 20 bytes")
    return raw


def message(address: str, nonce: int, prev_work, anchor) -> bytes:
    msg = address_bytes(address) + _bytes32(nonce) + _bytes32(prev_work) + _bytes32(anchor)
    assert len(msg) == MESSAGE_LEN
    return msg


def padded_block(msg: bytes) -> bytes:
    """The single 136-byte Keccak block for a 116-byte message (0x01 ... 0x80 padding)."""
    block = bytearray(KECCAK_RATE)
    block[: len(msg)] = msg
    block[len(msg)] ^= 0x01
    block[KECCAK_RATE - 1] ^= 0x80
    return bytes(block)


def digest(address: str, nonce: int, prev_work, anchor) -> bytes:
    return keccak(message(address, nonce, prev_work, anchor))


def digest_int(address: str, nonce: int, prev_work, anchor) -> int:
    return int.from_bytes(digest(address, nonce, prev_work, anchor), "big")


def meets(digest_value: bytes | int, target: int) -> bool:
    if isinstance(digest_value, bytes):
        digest_value = int.from_bytes(digest_value, "big")
    return digest_value < target


def verify(address: str, nonce: int, prev_work, anchor, target: int) -> bool:
    return meets(digest(address, nonce, prev_work, anchor), target)


def expected_hashes(target: int) -> int:
    if target <= 0:
        raise ValueError("target must be positive")
    return (1 << 256) // target


def cpu_search(address: str, prev_work, anchor, target: int, start: int, count: int):
    """Try `count` consecutive nonces from `start`. Returns (nonce, digest) or None."""
    addr = address_bytes(address)
    tail = _bytes32(prev_work) + _bytes32(anchor)
    for nonce in range(start, start + count):
        d = keccak(addr + nonce.to_bytes(32, "big") + tail)
        if int.from_bytes(d, "big") < target:
            return nonce, d
    return None


def random_start() -> int:
    """A nonce start with the low 64 bits clear, as the GPU worker requires."""
    return int.from_bytes(os.urandom(24), "big") << 64


def mine_calldata(nonce: int, anchor_block: int) -> bytes:
    """ABI calldata for mine(uint256 nonce, uint256 anchorBlock)."""
    selector = keccak(text="mine(uint256,uint256)")[:4]
    return selector + _bytes32(nonce) + _bytes32(anchor_block)
