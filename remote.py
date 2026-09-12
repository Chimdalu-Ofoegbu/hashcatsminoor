"""Where the GPU work runs: a box over SSH, or this machine."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

# Everything the GPU host needs. Wallet keys, state and secrets are never in this list.
REMOTE_FILES = ["remote_agent.py", "miner/Makefile", "miner/keccak.h", "miner/worker_common.h",
                "miner/cpu_worker.cpp", "miner/worker.cu", "scripts/remote_setup.sh",
                "scripts/gpu_selftest.py", "scripts/purekeccak.py"]


def shell_path(path: str) -> str:
    """Quote a path for a remote POSIX shell while letting a leading ~ expand there."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        rest = path[2:]
        return "~/" + shlex.quote(rest) if rest else "~/"
    return shlex.quote(path)


class Remote:
    def wait_for_ssh(self, timeout: float = 600, poll: float = 10) -> None:
        """Block until the host answers. Nothing to wait for on a local machine."""

    def run(self, cmd: str, timeout: float | None = None) -> tuple[int, str]:
        raise NotImplementedError

    def upload_repo(self, root: Path, dest: str) -> None:
        raise NotImplementedError

    def coordinator_kwargs(self, cpu: bool = False) -> dict:
        raise NotImplementedError


class SshRemote(Remote):
    def __init__(self, host: str, port: int = 22, user: str = "root", key_file: str | None = None,
                 extra_opts: str = "", log=print):
        self.host, self.port, self.user, self.extra_opts, self.log = host, port, user, extra_opts, log
        self.key_file = os.path.expanduser(key_file) if key_file else None

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def opts(self) -> list[str]:
        opts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=15",
                "-o", "ConnectTimeout=20"]
        if self.key_file:
            opts += ["-i", self.key_file]
        return opts + shlex.split(self.extra_opts)

    def ssh_cmd(self, remote_cmd: str) -> list[str]:
        return ["ssh", "-p", str(self.port), *self.opts(), self.target, remote_cmd]

    def run(self, cmd: str, timeout: float | None = None) -> tuple[int, str]:
        self.log(f"ssh {self.target}$ {cmd}")
        proc = subprocess.run(self.ssh_cmd(cmd), capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def wait_for_ssh(self, timeout: float = 600, poll: float = 10) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                rc, _ = self.run("true", timeout=30)
            except subprocess.TimeoutExpired:
                rc = -1
            if rc == 0:
                return
            time.sleep(poll)
        raise RuntimeError(f"ssh to {self.target}:{self.port} did not come up within {timeout:.0f}s")

    def upload_repo(self, root: Path, dest: str) -> None:
        self.log(f"upload {len(REMOTE_FILES)} files to {self.target}:{dest}")
        tar = subprocess.Popen(["tar", "czf", "-", "-C", str(root), *REMOTE_FILES], stdout=subprocess.PIPE)
        remote = f"mkdir -p {shell_path(dest)} && tar xzf - -C {shell_path(dest)}"
        proc = subprocess.run(self.ssh_cmd(remote), stdin=tar.stdout, capture_output=True, text=True, timeout=300)
        tar.wait()
        if tar.returncode != 0 or proc.returncode != 0:
            raise RuntimeError(f"upload failed: {proc.stderr.strip()[:300]}")

    def coordinator_kwargs(self, cpu: bool = False) -> dict:
        extra = self.extra_opts
        if self.key_file:
            extra = f"-i {shlex.quote(self.key_file)} " + extra
        return {"ssh_target": self.target, "ssh_port": self.port,
                "ssh_opts": ("-o StrictHostKeyChecking=accept-new " + extra).strip(), "local": False}


class LocalRemote(Remote):
    """The GPU (or the CPU rehearsal) is on this machine."""

    def __init__(self, workdir: Path, log=print):
        self.workdir = Path(workdir)
        self.log = log

    def run(self, cmd: str, timeout: float | None = None) -> tuple[int, str]:
        self.log(f"local {self.workdir}$ {cmd}")
        proc = subprocess.run(["bash", "-lc", cmd], cwd=self.workdir, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def upload_repo(self, root: Path, dest: str) -> None:
        dest_path = Path(dest).expanduser()
        if dest_path.resolve() == Path(root).resolve():
            return
        for rel in REMOTE_FILES:
            target = dest_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / rel, target)

    def coordinator_kwargs(self, cpu: bool = False) -> dict:
        worker = self.workdir / "miner" / ("cpu_worker" if cpu else "worker")
        return {"local": True, "worker": str(worker)}
