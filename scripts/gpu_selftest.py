#!/usr/bin/env python3
"""Validate and benchmark the worker binary on the GPU host. Standard library only.

    python3 scripts/gpu_selftest.py                 # every GPU nvidia-smi lists
    python3 scripts/gpu_selftest.py --gpus 2 --seconds 20
    python3 scripts/gpu_selftest.py --cpu --worker miner/cpu_worker --batch-log2 14

Checks, per worker process:
  1. digest mode against a pure-Python Keccak for random inputs
  2. an easy search finds a nonce that verifies and keeps the start's high bits
  3. an impossible search reports no hit and the full hash count
  4. throughput with an impossible target for --seconds
Exit status is non-zero if any check fails.
"""
import argparse
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from purekeccak import hashcats_digest  # noqa: E402


class Worker:
    def __init__(self, path, device, cpu):
        env = dict(os.environ)
        if not cpu:
            env["CUDA_DEVICE"] = str(device)
        self.proc = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     bufsize=1, env=env)

    def ask(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        out = self.proc.stdout.readline().strip()
        if not out:
            raise RuntimeError("worker exited")
        if out.startswith("error"):
            raise RuntimeError(out)
        return out.split()

    def close(self):
        try:
            self.proc.stdin.write("quit\n")
            self.proc.stdin.flush()
            self.proc.wait(5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def hx(v):
    return f"0x{v:064x}"


def check_worker(w, batch_log2, seconds, rng):
    addr = rng.randbytes(20)
    for _ in range(16):
        prev, anchor, nonce = rng.getrandbits(256), rng.getrandbits(256), rng.getrandbits(256)
        out = w.ask(f"digest 0x{addr.hex()} {hx(prev)} {hx(anchor)} {hx(nonce)}")
        want = "0x" + hashcats_digest(addr, nonce, prev, anchor).hex()
        if out[1] != want:
            raise RuntimeError(f"digest mismatch: got {out[1]} want {want}")
    prev, anchor = rng.getrandbits(256), rng.getrandbits(256)
    start = rng.getrandbits(192) << 64
    target = 1 << 240
    out = w.ask(f"job 0x{addr.hex()} {hx(prev)} {hx(anchor)} {hx(target)} {hx(start)} {1 << 20}")
    if out[1] != "1":
        raise RuntimeError("easy search found nothing")
    nonce = int(out[2], 16)
    if nonce >> 64 != start >> 64:
        raise RuntimeError("found nonce left the batch's nonce space")
    d = hashcats_digest(addr, nonce, prev, anchor)
    if int.from_bytes(d, "big") >= target or "0x" + d.hex() != out[3]:
        raise RuntimeError("found nonce does not verify")
    out = w.ask(f"job 0x{addr.hex()} {hx(prev)} {hx(anchor)} 0x1 {hx(start)} {1 << 20}")
    if out[1] != "0" or out[5] != str(1 << 20):
        raise RuntimeError(f"impossible search misreported: {' '.join(out)}")
    count = 1 << batch_log2
    hashes, t0 = 0, time.time()
    while time.time() - t0 < seconds:
        out = w.ask(f"job 0x{addr.hex()} {hx(prev)} {hx(anchor)} 0x1 {hx(start)} {count}")
        hashes += int(out[5])
    return hashes / (time.time() - t0)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p.add_argument("--worker", default=os.path.join(root, "miner", "worker"))
    p.add_argument("--gpus", type=int, default=0, help="0 = count from nvidia-smi")
    p.add_argument("--cpu", action="store_true")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--batch-log2", type=int, default=None)
    args = p.parse_args(argv)
    n = args.gpus
    if not n:
        if args.cpu:
            n = 1
        else:
            out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
            n = max(1, sum(1 for l in out.stdout.splitlines() if l.startswith("GPU")))
    batch = args.batch_log2 if args.batch_log2 is not None else (14 if args.cpu else 28)
    rng = random.Random()
    total = 0.0
    failed = False
    for i in range(n):
        w = Worker(args.worker, i, args.cpu)
        try:
            rate = check_worker(w, batch, args.seconds, rng)
            total += rate
            print(f"worker {i}: OK  {rate / 1e9:.3f} GH/s")
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"worker {i}: FAILED {exc}")
        finally:
            w.close()
    print(f"total {total / 1e9:.3f} GH/s over {n} worker(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
