"""Thin wrapper over the `vastai` command-line client (pip install vastai).

Every call shells out to the CLI with --raw and parses the reply, so the exact
HTTP API stays the CLI's problem. Written against vastai 1.7.0.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import time

DEFAULT_IMAGE = "nvidia/cuda:12.8.1-devel-ubuntu22.04"

# Keccak-256 hashes per second per card. The 5090 figure was reported by another
# miner; the rest are estimates scaled by SM count and clock. Measure before trusting.
GPU_GHS = {"RTX_5090": 6.4, "RTX_4090": 4.7, "RTX_3090": 2.4, "RTX_4080": 3.5, "RTX_5080": 4.0}


class VastError(RuntimeError):
    pass


def parse_reply(text: str):
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return ast.literal_eval(text)  # the CLI prints some replies as Python literals
    except (ValueError, SyntaxError) as exc:
        raise VastError(f"unparseable vastai reply: {text[:200]}") from exc


class VastCLI:
    def __init__(self, binary: str = "vastai", dry_run: bool = False, log=print, api_key: str | None = None):
        self.binary = binary
        self.dry_run = dry_run
        self.log = log
        self.api_key = api_key

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _run(self, args: list[str], raw: bool = True, timeout: float = 120):
        cmd = [self.binary, *args]
        if raw:
            cmd.append("--raw")
        if self.api_key:
            cmd += ["--api-key", self.api_key]
        shown = [a if a != self.api_key else "****" for a in cmd]
        self.log("vast$ " + " ".join(shown))
        if self.dry_run:
            return None
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise VastError(f"vastai {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
        return parse_reply(proc.stdout) if raw else proc.stdout.strip()

    def user(self) -> dict:
        return self._run(["show", "user"]) or {}

    def search(self, gpu: str, num_gpus: int, max_dph: float, min_cuda: float = 12.8,
               min_reliability: float = 0.95, min_inet_down: float = 100, disk: int = 20,
               extra: str = "") -> list[dict]:
        query = (f"gpu_name={gpu} num_gpus={num_gpus} cuda_vers>={min_cuda} reliability>{min_reliability} "
                 f"inet_down>{min_inet_down} disk_space>={disk} dph<={max_dph} {extra}").strip()
        offers = self._run(["search", "offers", query, "-o", "dph", "--storage", str(disk)]) or []
        out = []
        for o in offers:
            dph = o.get("dph_total", o.get("dph_base", o.get("dph")))
            if dph is None:
                continue
            out.append({
                "id": o["id"], "dph": float(dph), "gpu_name": o.get("gpu_name"), "num_gpus": o.get("num_gpus"),
                "cuda": o.get("cuda_max_good"), "reliability": o.get("reliability2", o.get("reliability")),
                "geolocation": o.get("geolocation"), "inet_down": o.get("inet_down"), "disk": o.get("disk_space"),
            })
        return sorted(out, key=lambda o: o["dph"])

    def create(self, offer_id: int, image: str = DEFAULT_IMAGE, disk: int = 20, label: str = "hashcats",
               onstart_cmd: str | None = None) -> int:
        args = ["create", "instance", str(offer_id), "--image", image, "--disk", str(disk), "--ssh", "--direct",
                "--label", label, "--cancel-unavail"]
        if onstart_cmd:
            args += ["--onstart-cmd", onstart_cmd]
        reply = self._run(args)
        if self.dry_run:
            return 0
        if not reply or not reply.get("success"):
            raise VastError(f"create instance refused: {reply}")
        return int(reply["new_contract"])

    def instance(self, instance_id: int) -> dict:
        return self._run(["show", "instance", str(instance_id)]) or {}

    def ssh_endpoint(self, instance_id: int) -> tuple[str, str, int]:
        """(user, host, port) for the instance, from `vastai ssh-url`."""
        url = self._run(["ssh-url", str(instance_id)], raw=False) or ""
        m = re.search(r"ssh://([^@]+)@([^:\s]+):(\d+)", url)
        if not m:
            raise VastError(f"could not parse ssh url: {url!r}")
        return m.group(1), m.group(2), int(m.group(3))

    def wait_running(self, instance_id: int, timeout: float = 900, poll: float = 10) -> dict:
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            last = self.instance(instance_id)
            status = last.get("actual_status") or last.get("cur_state")
            if status == "running":
                return last
            if status in ("exited", "error"):
                raise VastError(f"instance {instance_id} entered {status}: {last.get('status_msg')}")
            self.log(f"instance {instance_id}: {status or 'scheduling'} ...")
            time.sleep(poll)
        raise VastError(f"instance {instance_id} not running after {timeout:.0f}s")

    def destroy(self, instance_id: int) -> None:
        self._run(["destroy", "instance", str(instance_id), "-y"])
