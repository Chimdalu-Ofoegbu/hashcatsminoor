import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gpu_selftest  # noqa: E402
import purekeccak  # noqa: E402


def test_pure_keccak_matches_eth_utils():
    rng = random.Random(3)
    assert purekeccak.keccak256(b"") == keccak(b"")
    for n in (1, 55, 116, 135, 136, 137, 271, 272, 500):
        data = rng.randbytes(n)
        assert purekeccak.keccak256(data) == keccak(data), n


def test_hashcats_digest_matches_reference():
    import pow as hpow
    addr = bytes.fromhex("ab" * 20)
    assert purekeccak.hashcats_digest(addr, 5, 6, 7) == hpow.digest("0x" + addr.hex(), 5, 6, 7)


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not available")
def test_selftest_passes_on_cpu_worker(tmp_path, capsys):
    binary = tmp_path / "cpu_worker"
    subprocess.run(["g++", "-O2", "-std=c++17", "-o", str(binary), str(ROOT / "miner" / "cpu_worker.cpp")], check=True)
    assert gpu_selftest.main(["--cpu", "--worker", str(binary), "--gpus", "1", "--seconds", "0.3",
                              "--batch-log2", "12"]) == 0
    out = capsys.readouterr().out
    assert "worker 0: OK" in out and "total" in out


def test_selftest_flags_a_broken_worker(tmp_path, capsys):
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\nwhile read line; do echo digest 0x00; done\n")
    bad.chmod(0o755)
    assert gpu_selftest.main(["--cpu", "--worker", str(bad), "--gpus", "1", "--seconds", "0.1"]) == 1
    assert "FAILED digest mismatch" in capsys.readouterr().out
