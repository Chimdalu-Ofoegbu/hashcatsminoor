import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import pow as hpow

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not available")


@pytest.fixture(scope="module")
def cpu_worker(tmp_path_factory):
    binary = tmp_path_factory.mktemp("build") / "cpu_worker"
    subprocess.run(["g++", "-O2", "-std=c++17", "-o", str(binary), str(ROOT / "miner" / "cpu_worker.cpp")], check=True)
    return binary


def read_until(proc, wanted, limit=200):
    for _ in range(limit):
        line = proc.stdout.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg["type"] == wanted:
            return msg
    raise AssertionError(f"no {wanted} message")


def test_agent_finds_candidates_and_stops(cpu_worker):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "remote_agent.py"), "--worker", str(cpu_worker), "--cpu",
         "--gpus", "2", "--batch-log2", "10", "--stats-seconds", "0.2"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        assert read_until(proc, "ready")["workers"] == 2
        addr, prev, anchor, target = "0x" + "ab" * 20, 3 << 200, 4 << 100, 1 << 250
        job = {"type": "job", "id": 7, "address": addr, "prev": f"0x{prev:064x}",
               "anchor": f"0x{anchor:064x}", "target": f"0x{target:064x}"}
        proc.stdin.write(json.dumps(job) + "\n")
        proc.stdin.flush()
        cand = read_until(proc, "candidate")
        assert cand["job"] == 7 and cand["worker"] in (0, 1)
        nonce = int(cand["nonce"], 16)
        assert hpow.verify(addr, nonce, prev, anchor, target)
        assert bytes.fromhex(cand["digest"][2:]) == hpow.digest(addr, nonce, prev, anchor)
        assert nonce >> 200 in (1, 2)
        stats = read_until(proc, "stats")
        assert stats["job"] == 7 and len(stats["hashrate"]) == 2 and stats["hashes"] > 0
        # One candidate per job unless the coordinator asks for another.
        proc.stdin.write(json.dumps({"type": "resume"}) + "\n")
        proc.stdin.flush()
        again = read_until(proc, "candidate")
        assert again["job"] == 7 and again["nonce"] != cand["nonce"]
        # A new job id reports again without a resume.
        job["id"] = 8
        proc.stdin.write(json.dumps(job) + "\n")
        proc.stdin.flush()
        assert read_until(proc, "candidate")["job"] == 8
        proc.stdin.write(json.dumps({"type": "stop"}) + "\n")
        proc.stdin.flush()
        assert read_until(proc, "stopped")
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_agent_reports_worker_error(tmp_path):
    bad = tmp_path / "bad_worker.sh"
    bad.write_text("#!/bin/sh\nread line\necho error nope\n")
    bad.chmod(0o755)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "remote_agent.py"), "--worker", str(bad), "--cpu", "--gpus", "1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        read_until(proc, "ready")
        proc.stdin.write(json.dumps({"type": "job", "id": 1, "address": "0x" + "00" * 20,
                                     "prev": "0x0", "anchor": "0x0", "target": "0x1"}) + "\n")
        proc.stdin.flush()
        err = read_until(proc, "worker_error")
        assert "nope" in err["text"]
        assert read_until(proc, "stopped")
    finally:
        if proc.poll() is None:
            proc.kill()
