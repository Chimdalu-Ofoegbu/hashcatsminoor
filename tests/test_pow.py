import pytest
from eth_utils import keccak

import pow as hpow

ADDR = "0x" + "ab" * 20
PREV = 0x1111 << 200
ANCHOR = 0x2222 << 100


def test_keccak_backend_is_ethereum_keccak_not_sha3():
    assert keccak(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def test_message_layout():
    msg = hpow.message(ADDR, 5, PREV, ANCHOR)
    assert len(msg) == 116
    assert msg[:20] == bytes.fromhex("ab" * 20)
    assert msg[20:52] == (5).to_bytes(32, "big")
    assert msg[52:84] == PREV.to_bytes(32, "big")
    assert msg[84:] == ANCHOR.to_bytes(32, "big")


def test_padded_block():
    block = hpow.padded_block(hpow.message(ADDR, 0, 0, 0))
    assert len(block) == 136
    assert block[116] == 0x01
    assert block[135] == 0x80
    assert all(b == 0 for b in block[117:135])


def test_digest_matches_direct_keccak():
    assert hpow.digest(ADDR, 7, PREV, ANCHOR) == keccak(hpow.message(ADDR, 7, PREV, ANCHOR))


def test_verify_and_meets():
    d = hpow.digest_int(ADDR, 7, PREV, ANCHOR)
    assert hpow.verify(ADDR, 7, PREV, ANCHOR, d + 1)
    assert not hpow.verify(ADDR, 7, PREV, ANCHOR, d)


def test_cpu_search_finds_a_valid_nonce():
    target = 1 << 250  # about one hit per 64 hashes
    found = hpow.cpu_search(ADDR, PREV, ANCHOR, target, 1000, 2000)
    assert found is not None
    nonce, d = found
    assert 1000 <= nonce < 3000
    assert hpow.verify(ADDR, nonce, PREV, ANCHOR, target)
    assert d == hpow.digest(ADDR, nonce, PREV, ANCHOR)


def test_cpu_search_can_miss():
    assert hpow.cpu_search(ADDR, PREV, ANCHOR, 1, 0, 50) is None


def test_expected_hashes():
    assert hpow.expected_hashes(1 << 255) == 2
    assert hpow.expected_hashes(1 << 200) == 1 << 56
    with pytest.raises(ValueError):
        hpow.expected_hashes(0)


def test_random_start_has_clear_low_bits():
    for _ in range(20):
        s = hpow.random_start()
        assert s & ((1 << 64) - 1) == 0
        assert s < 1 << 256


def test_mine_calldata():
    data = hpow.mine_calldata(3, 99)
    assert data[:4] == keccak(text="mine(uint256,uint256)")[:4]
    assert data[4:36] == (3).to_bytes(32, "big")
    assert data[36:] == (99).to_bytes(32, "big")
    assert len(data) == 68
