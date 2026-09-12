#!/usr/bin/env python3
"""ssh ProxyCommand that tunnels through an HTTP CONNECT proxy.

    ssh -o ProxyCommand="python3 scripts/proxy_connect.py %h %p" user@host

Reads the proxy from HTTPS_PROXY (or the first argument pair after --proxy).
Standard library only.
"""
import os
import select
import socket
import sys
import urllib.parse


def main():
    args = sys.argv[1:]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if args[:1] == ["--proxy"]:
        proxy, args = args[1], args[2:]
    host, port = args[0], int(args[1])
    if not proxy:
        sys.stderr.write("proxy_connect: no proxy configured\n")
        return 2
    p = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
    sock = socket.create_connection((p.hostname, p.port or 3128), timeout=30)
    sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            sys.stderr.write("proxy_connect: proxy closed the connection\n")
            return 1
        head += chunk
    status, _, rest = head.partition(b"\r\n\r\n")
    status_line = status.split(b"\r\n")[0].decode(errors="replace")
    if " 200 " not in status_line:
        sys.stderr.write(f"proxy_connect: {status_line}\n")
        return 1
    sock.settimeout(None)
    out = sys.stdout.buffer
    if rest:
        out.write(rest)
        out.flush()
    stdin = sys.stdin.buffer
    while True:
        watch = [sock] if stdin is None else [sock, stdin]
        readable, _, _ = select.select(watch, [], [])
        if sock in readable:
            data = sock.recv(65536)
            if not data:
                return 0
            out.write(data)
            out.flush()
        if stdin in readable:
            data = os.read(stdin.fileno(), 65536)
            if not data:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    return 0  # peer already gone
                stdin = None  # stop selecting on a spent stdin
                continue
            try:
                sock.sendall(data)
            except OSError:
                return 0


if __name__ == "__main__":
    sys.exit(main())
