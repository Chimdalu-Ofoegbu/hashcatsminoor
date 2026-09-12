#!/usr/bin/env python3
"""Runs on the GPU host. Standard library only, so a bare rented box can run it.

It owns one worker process per GPU (miner/worker, or miner/cpu_worker with --cpu),
takes jobs from stdin, and reports candidates and hashrate on stdout. It never
touches the chain or a wallet: the coordinator on your own machine does that.

stdin, one JSON object per line:
    {"type": "job", "id": 7, "address": "0x..", "prev": "0x..", "anchor": "0x..", "target": "0x.."}
    {"type": "resume"}      (the last candidate failed: report the next one for the same job)
    {"type": "stop"}
stdout, one JSON object per line:
    {"type": "ready", "workers": 4}
    {"type": "candidate", "job": 7, "worker": 0, "nonce": "0x..", "digest": "0x.."}
    {"type": "stats", "job": 7, "hashrate": [..per worker H/s..], "hashes": 123}
    {"type": "worker_error", "worker": 0, "text": ".."}
    {"type": "stopped"}
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

emit_lock = threading.Lock()


def emit(obj):
    with emit_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


class Agent:
    def __init__(self, worker_path, workers, batch_log2, cpu, stats_seconds):
        self.worker_path = worker_path
        self.n = workers
        self.count = 1 << batch_log2
        self.cpu = cpu
        self.stats_seconds = stats_seconds
        self.job = None
        self.job_lock = threading.Lock()
        self.job_event = threading.Event()
        self.stop = threading.Event()
        self.rates = [0.0] * workers
        self.hashes = [0] * workers
        self.salt = int.from_bytes(os.urandom(8), "big")
        self.procs = []
        self.reported = None  # job id whose candidate was already reported

    def should_report(self, job_id):
        with self.job_lock:
            if self.reported == job_id:
                return False
            self.reported = job_id
            return True

    def resume(self):
        with self.job_lock:
            self.reported = None

    def current_job(self):
        with self.job_lock:
            return self.job

    def set_job(self, job):
        with self.job_lock:
            self.job = job
        self.job_event.set()

    def spawn(self, idx):
        env = dict(os.environ)
        if not self.cpu:
            env["CUDA_DEVICE"] = str(idx)
        proc = subprocess.Popen(
            [self.worker_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if self.cpu else sys.stderr, text=True, bufsize=1, env=env,
        )
        self.procs.append(proc)
        return proc

    def worker_loop(self, idx):
        proc = self.spawn(idx)
        seq = 0
        try:
            while not self.stop.is_set():
                job = self.current_job()
                if job is None:
                    self.job_event.wait(0.2)
                    continue
                # Unique nonce space per worker, per process start, per batch.
                start = ((idx + 1) << 200) | (self.salt << 128) | (seq << 64)
                seq += 1
                line = (f"job {job['address']} {job['prev']} {job['anchor']} {job['target']} "
                        f"0x{start:064x} {self.count}\n")
                proc.stdin.write(line)
                proc.stdin.flush()
                out = proc.stdout.readline()
                if not out:
                    raise RuntimeError("worker exited")
                fields = out.split()
                if fields[0] != "result" or len(fields) < 6:
                    raise RuntimeError(out.strip())
                ms = float(fields[4])
                done = int(fields[5])
                self.hashes[idx] += done
                if ms > 0:
                    self.rates[idx] = done / ms * 1000
                if fields[1] == "1" and self.should_report(job["id"]):
                    emit({"type": "candidate", "job": job["id"], "worker": idx,
                          "nonce": fields[2], "digest": fields[3]})
        except Exception as exc:  # noqa: BLE001 - report and stop everything
            emit({"type": "worker_error", "worker": idx, "text": str(exc)})
            self.stop.set()
        finally:
            try:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
            except Exception:  # noqa: BLE001
                pass
            proc.terminate()

    def stdin_loop(self):
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "job":
                self.set_job(msg)
            elif msg.get("type") == "resume":
                self.resume()
            elif msg.get("type") == "stop":
                break
        self.stop.set()
        self.job_event.set()

    def run(self):
        threads = [threading.Thread(target=self.worker_loop, args=(i,), daemon=True) for i in range(self.n)]
        for t in threads:
            t.start()
        threading.Thread(target=self.stdin_loop, daemon=True).start()
        emit({"type": "ready", "workers": self.n})
        last = time.time()
        while not self.stop.wait(0.1):
            if time.time() - last >= self.stats_seconds:
                last = time.time()
                job = self.current_job()
                emit({"type": "stats", "job": job["id"] if job else None,
                      "hashrate": list(self.rates), "hashes": sum(self.hashes)})
        for t in threads:
            t.join(5)
        for p in self.procs:
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()
        emit({"type": "stopped"})


def detect_gpus():
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10, check=True)
        return max(1, sum(1 for line in out.stdout.splitlines() if line.startswith("GPU")))
    except Exception:  # noqa: BLE001
        return 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--worker", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "miner", "worker"))
    p.add_argument("--gpus", type=int, default=0, help="worker processes; 0 = one per GPU from nvidia-smi")
    p.add_argument("--cpu", action="store_true", help="the worker is the CPU build (no CUDA_DEVICE env)")
    p.add_argument("--batch-log2", type=int, default=None, help="hashes per batch as a power of two")
    p.add_argument("--stats-seconds", type=float, default=10.0)
    args = p.parse_args(argv)
    workers = args.gpus or (1 if args.cpu else detect_gpus())
    batch = args.batch_log2 if args.batch_log2 is not None else (16 if args.cpu else 28)
    Agent(args.worker, workers, batch, args.cpu, args.stats_seconds).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
