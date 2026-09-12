"""Compile the CPU worker and check it against the Python reference."""
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

import pow as hpow

ROOT = Path(__file__).resolve().parents[1]
MINER = ROOT / "miner"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not available")


@pytest.fixture(scope="module")
def worker(tmp_path_factory):
    binary = tmp_path_factory.mktemp("build") / "cpu_worker"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Werror", "-o", str(binary), str(MINER / "cpu_worker.cpp")],
        check=True,
    )
    return binary


def run(binary, lines):
    proc = subprocess.run([str(binary)], input="\n".join(lines) + "\nquit\n", text=True,
                          capture_output=True, timeout=120, check=True)
    return proc.stdout.strip().splitlines()


def hx(v, n=32):
    return "0x" + v.to_bytes(n, "big").hex()


def test_digest_matches_python_for_random_nonces(worker):
    rng = random.Random(1)
    lines, expected = [], []
    for _ in range(64):
        addr = "0x" + rng.randbytes(20).hex()
        prev = rng.getrandbits(256)
        anchor = rng.getrandbits(256)
        nonce = rng.getrandbits(256)
        lines.append(f"digest {addr} {hx(prev)} {hx(anchor)} {hx(nonce)}")
        expected.append("digest 0x" + hpow.digest(addr, nonce, prev, anchor).hex())
    assert run(worker, lines) == expected


def test_digest_accepts_short_hex_and_zero_nonce(worker):
    addr = "0x" + "ab" * 20
    out = run(worker, [f"digest {addr} 0 0 0"])
    assert out == ["digest 0x" + hpow.digest(addr, 0, 0, 0).hex()]


def test_search_finds_smallest_valid_offset(worker):
    addr = "0x" + "11" * 20
    prev, anchor = 5 << 100, 6 << 100
    target = 1 << 250
    start = 0xABCDEF << 64
    out = run(worker, [f"job {addr} {hx(prev)} {hx(anchor)} {hx(target)} {hx(start)} 4096"])
    fields = out[0].split()
    assert fields[:2] == ["result", "1"]
    nonce = int(fields[2], 16)
    digest = bytes.fromhex(fields[3][2:])
    assert nonce >> 64 == 0xABCDEF
    assert hpow.verify(addr, nonce, prev, anchor, target)
    assert digest == hpow.digest(addr, nonce, prev, anchor)
    expected = hpow.cpu_search(addr, prev, anchor, target, start, 4096)
    assert expected[0] == nonce
    assert int(fields[5]) == nonce - start + 1


def test_search_reports_miss_and_hash_count(worker):
    addr = "0x" + "22" * 20
    out = run(worker, [f"job {addr} 0 0 1 0 5000"])
    fields = out[0].split()
    assert fields[:2] == ["result", "0"]
    assert fields[5] == "5000"


def test_rejects_bad_start_and_unknown_command(worker):
    addr = "0x" + "22" * 20
    out = run(worker, [f"job {addr} 0 0 1 1 10", "bogus 1 2", "job onlytwo fields"])
    assert out[0].startswith("error start must have its low 64 bits clear")
    assert out[1].startswith("error unknown command")
    assert out[2].startswith("error job needs 6 fields")


def test_multi_word_batch_boundary(worker):
    """Offsets above 2^32 inside the low 64 bits still hash correctly."""
    addr = "0x" + "33" * 20
    nonce = (7 << 64) | (1 << 40) | 12345
    out = run(worker, [f"digest {addr} 0 0 {hx(nonce)}"])
    assert out == ["digest 0x" + hpow.digest(addr, nonce, 0, 0).hex()]
