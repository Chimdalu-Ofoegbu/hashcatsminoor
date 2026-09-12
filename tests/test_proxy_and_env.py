import json
import os
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path

import remote as remote_mod
import vast as vast_mod

ROOT = Path(__file__).resolve().parents[1]


def test_ssh_uses_proxy_command_only_when_a_proxy_is_set(monkeypatch):
    monkeypatch.delenv("SSH_PROXY_COMMAND", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("SSH_VIA_PROXY", "1")
    r = remote_mod.SshRemote("h", 22, log=lambda *_: None)
    assert not any("ProxyCommand" in o for o in r.ssh_cmd("true"))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    cmd = " ".join(r.ssh_cmd("true"))
    assert "ProxyCommand=" in cmd and "proxy_connect.py %h %p" in cmd
    assert "proxy_connect.py" in r.coordinator_kwargs()["ssh_opts"]
    monkeypatch.setenv("SSH_VIA_PROXY", "0")
    assert not any("ProxyCommand" in o for o in r.ssh_cmd("true"))
    monkeypatch.setenv("SSH_PROXY_COMMAND", "corkscrew p 1 %h %p")
    assert "ProxyCommand=corkscrew p 1 %h %p" in r.ssh_cmd("true")


def _fake_connect_proxy(banner: bytes):
    """A tiny CONNECT proxy: answers 200, then echoes the banner and mirrors input."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = {}

    def serve():
        conn, _ = srv.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        seen["request"] = head.split(b"\r\n")[0].decode()
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n" + banner)
        data = conn.recv(4096)
        conn.sendall(b"echo:" + data)
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], seen


def test_proxy_connect_tunnels_bytes_both_ways():
    port, seen = _fake_connect_proxy(b"SSH-2.0-fake\r\n")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "proxy_connect.py"), "--proxy",
                           f"http://127.0.0.1:{port}", "example.invalid", "22"],
                          input=b"hello", capture_output=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert seen["request"] == "CONNECT example.invalid:22 HTTP/1.1"
    assert proc.stdout == b"SSH-2.0-fake\r\necho:hello"


def test_proxy_connect_reports_a_refused_tunnel():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "proxy_connect.py"), "--proxy",
                           f"http://127.0.0.1:{srv.getsockname()[1]}", "blocked.invalid", "22"],
                          input=b"", capture_output=True, timeout=20)
    assert proc.returncode == 1 and b"403" in proc.stderr


def test_vast_cli_takes_api_key_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VAST_API_KEY", "sekrit")
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "vastai"
    fake.write_text(f"#!{sys.executable}\nimport sys\nopen({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nprint('{{}}')\n")
    fake.chmod(0o755)
    shown = []
    cli = vast_mod.VastCLI(binary=str(fake), log=shown.append)
    cli.user()
    assert "--api-key sekrit" in calls.read_text()
    assert "sekrit" not in " ".join(shown) and "****" in " ".join(shown)
    monkeypatch.delenv("VAST_API_KEY")
    assert vast_mod.VastCLI(binary=str(fake), log=shown.append).api_key is None
