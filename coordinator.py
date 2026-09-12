#!/usr/bin/env python3
"""Local coordinator: watches the chain, feeds jobs to the GPU agent, verifies
candidates, signs mine() transactions, and records what actually minted.

Runs on your own machine. The wallet key is read here and never leaves.
Default is a dry run: everything except signing and broadcasting. Pass --live
to spend ETH.

Environment (or a .env file you export yourself):
    RPC_URL, COLLECTION_ADDRESS      chain (defaults: official RPC, Hashcats contract)
    MINER_ADDRESS, WALLET_KEY_FILE   your address and a file holding its hex private key
    SSH_TARGET, SSH_PORT, SSH_OPTS   GPU host as user@host (omit for --local-cpu)
    REMOTE_DIR, REMOTE_PYTHON        where this repo lives on the GPU host, python there
    GPUS, BATCH_LOG2                 worker processes (0 = detect), hashes per batch
    MAX_PRICE_ETH, TARGET_MINTS      entry-price ceiling, cats to mint this session
    MAX_SPEND_ETH, MAX_HOURS         hard stops on spend (price + gas) and wall time
    STATE_DIR                        logs, receipts and the pending-transaction marker
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from eth_account import Account
from eth_utils import keccak

import chain
import pow as hpow

HERE = Path(__file__).resolve().parent
GAS_LIMIT = 400_000
MIN_GAS_PRICE = 100_000_000  # 0.1 gwei


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


class Config:
    def __init__(self, **kw):
        self.rpc_url = kw.get("rpc_url") or chain.RPC_URL
        self.collection = kw.get("collection") or chain.COLLECTION
        self.key_file = kw.get("key_file") or env("WALLET_KEY_FILE", "secrets/wallet.key")
        self.miner_address = kw.get("miner_address") or env("MINER_ADDRESS")
        self.ssh_target = kw.get("ssh_target", env("SSH_TARGET"))
        self.ssh_port = int(kw.get("ssh_port") or env("SSH_PORT", "22"))
        self.ssh_opts = kw.get("ssh_opts") or env("SSH_OPTS", "")
        self.remote_dir = kw.get("remote_dir") or env("REMOTE_DIR", "~/hashcatsminoor")
        self.remote_python = kw.get("remote_python") or env("REMOTE_PYTHON", "python3")
        self.gpus = int(kw.get("gpus") if kw.get("gpus") is not None else env("GPUS", "0"))
        self.batch_log2 = kw.get("batch_log2")
        self.local_cpu = bool(kw.get("local_cpu", False))
        self.worker = kw.get("worker")
        self.max_price_wei = int(float(kw.get("max_price_eth") or env("MAX_PRICE_ETH", "0")) * 1e18)
        self.target_mints = int(kw.get("target_mints") or env("TARGET_MINTS", "1"))
        self.max_spend_wei = int(float(kw.get("max_spend_eth") or env("MAX_SPEND_ETH", "0.05")) * 1e18)
        self.max_hours = float(kw.get("max_hours") or env("MAX_HOURS", "2"))
        self.state_dir = Path(kw.get("state_dir") or env("STATE_DIR", "state"))
        self.poll_ms = int(kw.get("poll_ms") or env("POLL_MS", "150"))
        self.live = bool(kw.get("live", False))
        self.max_reverts = int(kw.get("max_reverts") or env("MAX_REVERTS", "5"))
        self.receipt_timeout = float(kw.get("receipt_timeout") or env("RECEIPT_TIMEOUT", "90"))
        self.stats_seconds = float(kw.get("stats_seconds") or env("STATS_SECONDS", "10"))

    def agent_command(self):
        batch = self.batch_log2 if self.batch_log2 is not None else (16 if self.local_cpu else 28)
        if self.local_cpu:
            worker = self.worker or str(HERE / "miner" / "cpu_worker")
            return [sys.executable, str(HERE / "remote_agent.py"), "--worker", worker, "--cpu",
                    "--gpus", str(self.gpus or 1), "--batch-log2", str(batch),
                    "--stats-seconds", str(self.stats_seconds)]
        if not self.ssh_target:
            raise RuntimeError("set SSH_TARGET (user@host) or use --local-cpu")
        remote = (f"cd {shlex.quote(self.remote_dir)} && {shlex.quote(self.remote_python)} -u remote_agent.py "
                  f"--worker ./miner/worker --gpus {self.gpus} --batch-log2 {batch} "
                  f"--stats-seconds {self.stats_seconds}")
        cmd = ["ssh", "-p", str(self.ssh_port), "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15"]
        cmd += shlex.split(self.ssh_opts)
        cmd += [self.ssh_target, remote]
        return cmd


def load_account(cfg):
    if not cfg.live:
        return None
    lines = [l.strip() for l in Path(cfg.key_file).read_text().splitlines() if l.strip()]
    if not lines:
        raise RuntimeError("wallet key file is empty")
    account = Account.from_key(lines[0])
    if cfg.miner_address and account.address.lower() != cfg.miner_address.lower():
        raise RuntimeError("MINER_ADDRESS does not match the key file")
    return account


class Coordinator:
    def __init__(self, cfg: Config, account=None, chain_obj=None):
        self.cfg = cfg
        self.account = account
        self.address = account.address if account else cfg.miner_address
        if not self.address:
            raise RuntimeError("set MINER_ADDRESS (or a key file with --live)")
        self.chain = chain_obj or chain.Chain(cfg.rpc_url, cfg.collection)
        self.events = queue.Queue()
        self.agent = None
        self.job = None
        self.job_id = 0
        self.submitted_jobs = set()
        self.minted = []
        self.spend_wei = 0
        self.reverts = 0
        self.hashrate = []
        self.started = time.time()
        self.stop_reason = None
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = cfg.state_dir / "log.jsonl"
        self.receipts_file = cfg.state_dir / "receipts.json"
        self.pending_file = cfg.state_dir / "pending.json"
        if self.receipts_file.exists():
            self.minted = json.loads(self.receipts_file.read_text())

    # ---- logging ----
    def log(self, kind, **fields):
        record = {"at": round(time.time(), 3), "type": kind, **fields}
        with self.log_file.open("a") as f:
            f.write(json.dumps(record) + "\n")
        summary = " ".join(f"{k}={v}" for k, v in fields.items() if k not in ("tx_raw",))
        print(f"[{time.strftime('%H:%M:%S')}] {kind} {summary}", flush=True)

    def stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
            self.log("stopping", reason=reason)

    # ---- agent ----
    def start_agent(self):
        cmd = self.cfg.agent_command()
        self.log("agent_start", command=" ".join(shlex.quote(c) for c in cmd))
        self.agent = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      text=True, bufsize=1)
        threading.Thread(target=self._read_agent, daemon=True).start()

    def _read_agent(self):
        for line in self.agent.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                self.events.put({"type": "agent_noise", "text": line.strip()})
        self.events.put({"type": "agent_eof"})

    def send(self, msg):
        try:
            self.agent.stdin.write(json.dumps(msg) + "\n")
            self.agent.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            self.stop("agent pipe closed")

    def stop_agent(self):
        if not self.agent:
            return
        self.send({"type": "stop"})
        try:
            self.agent.wait(15)
        except subprocess.TimeoutExpired:
            self.agent.kill()

    # ---- jobs ----
    def refresh_job(self, snap):
        key = (snap["prev"], snap["anchor_block"])
        if self.job and (self.job["prev"], self.job["anchor_block"]) == key:
            return
        self.job_id += 1
        self.job = {"id": self.job_id, **snap}
        self.send({"type": "job", "id": self.job_id, "address": self.address,
                   "prev": f"0x{snap['prev']:064x}", "anchor": f"0x{snap['anchor']:064x}",
                   "target": f"0x{snap['target']:064x}"})
        bits = hpow.expected_hashes(snap["target"]).bit_length() - 1 if snap["target"] else 0
        self.log("job", id=self.job_id, minted=snap["total_minted"], price_eth=snap["price_wei"] / 1e18,
                 expected_hashes=f"2^{bits}", anchor_block=snap["anchor_block"])

    # ---- candidates ----
    def handle_candidate(self, ev):
        job = self.job
        if not job or ev["job"] != job["id"]:
            self.log("stale_candidate", job=ev["job"], current=job["id"] if job else None)
            return
        if job["id"] in self.submitted_jobs:
            return
        nonce = int(ev["nonce"], 16)
        if not hpow.verify(self.address, nonce, job["prev"], job["anchor"], job["target"]):
            self.log("bad_candidate", nonce=ev["nonce"], worker=ev.get("worker"))
            self.stop("worker produced an invalid candidate; do not trust this build")
            return
        if self.chain.prev_work() != job["prev"]:
            self.log("stale_candidate", job=job["id"], reason="prevWork moved")
            return
        price = self.chain.mint_price()
        if price > self.cfg.max_price_wei:
            self.stop(f"mint price {price / 1e18:.4f} ETH above MAX_PRICE_ETH")
            return
        gas_price = max(self.chain.gas_price() * 2, MIN_GAS_PRICE)
        cost = price + GAS_LIMIT * gas_price
        if self.spend_wei + cost > self.cfg.max_spend_wei:
            self.stop("MAX_SPEND_ETH would be exceeded")
            return
        balance = self.chain.balance(self.address)
        if balance < cost:
            self.stop("insufficient balance for price plus gas")
            return
        tx = {
            "chainId": chain.CHAIN_ID,
            "nonce": self.chain.tx_count(self.address),
            "to": self.cfg.collection,
            "value": price,
            "gas": GAS_LIMIT,
            "gasPrice": gas_price,
            "data": "0x" + hpow.mine_calldata(nonce, job["anchor_block"]).hex(),
        }
        self.submitted_jobs.add(job["id"])
        if not self.cfg.live:
            self.minted.append({"dry_run": True, "job": job["id"], "nonce": ev["nonce"], "price_wei": price,
                                "anchor_block": job["anchor_block"]})
            self.log("would_submit", job=job["id"], nonce=ev["nonce"], price_eth=price / 1e18,
                     dry_mints=len(self.minted))
            return
        self.submit(tx, job, nonce, price)

    def submit(self, tx, job, nonce, price):
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        txhash = "0x" + keccak(bytes(raw)).hex()
        self.pending_file.write_text(json.dumps({"tx": txhash, "nonce": tx["nonce"], "job": job["id"],
                                                 "price_wei": price, "at": time.time()}))
        self.log("broadcast", tx=txhash, job=job["id"], price_eth=price / 1e18)
        try:
            self.chain.send_raw(bytes(raw))
        except Exception as exc:  # noqa: BLE001 - the tx may still have gone out
            self.log("broadcast_uncertain", tx=txhash, error=str(exc))
        receipt = None
        deadline = time.time() + self.cfg.receipt_timeout
        while time.time() < deadline:
            try:
                receipt = self.chain.receipt(txhash)
            except Exception:  # noqa: BLE001
                receipt = None
            if receipt:
                break
            time.sleep(0.25)
        if not receipt:
            self.stop("receipt unresolved; pending.json kept for manual reconciliation")
            return
        gas_cost = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
        if int(receipt["status"], 16) == 1:
            ids = chain.minted_token_ids(receipt, self.cfg.collection, self.address)
            owner_ok = len(ids) == 1 and self.chain.owner_of(ids[0]).lower() == self.address.lower()
            if not owner_ok:
                self.pending_file.unlink(missing_ok=True)
                self.stop(f"mint receipt succeeded but ownership check failed for ids {ids}")
                return
            self.spend_wei += price + gas_cost
            record = {"token_id": ids[0], "tx": txhash, "price_wei": price, "gas_wei": gas_cost,
                      "nonce": f"0x{nonce:064x}", "at": time.time()}
            self.minted.append(record)
            self.receipts_file.write_text(json.dumps(self.minted, indent=2))
            self.log("MINTED", token_id=ids[0], tx=txhash, count=len(self.minted),
                     spend_eth=self.spend_wei / 1e18)
        else:
            self.spend_wei += gas_cost
            self.reverts += 1
            self.log("reverted", tx=txhash, reverts=self.reverts)
            if self.reverts >= self.cfg.max_reverts:
                self.stop("too many reverted transactions")
            else:
                self.submitted_jobs.discard(job["id"])
                self.send({"type": "resume"})
        self.pending_file.unlink(missing_ok=True)

    # ---- main loop ----
    def status(self, snap):
        ghs = sum(self.hashrate) / 1e9 if self.hashrate else 0.0
        expected = hpow.expected_hashes(snap["target"]) if snap["target"] else 0
        minutes = expected / (ghs * 1e9) / 60 if ghs else float("inf")
        self.log("status", minted=len(self.minted), target=self.cfg.target_mints,
                 spend_eth=round(self.spend_wei / 1e18, 6), ghs=round(ghs, 2),
                 solo_minutes_per_cat=round(minutes, 1), total_minted=snap["total_minted"],
                 price_eth=snap["price_wei"] / 1e18, uptime_min=round((time.time() - self.started) / 60, 1))

    def run(self):
        cfg = self.cfg
        if self.pending_file.exists():
            raise RuntimeError(f"{self.pending_file} exists: reconcile that transaction before restarting")
        if self.chain.chain_id() != chain.CHAIN_ID:
            raise RuntimeError("RPC is not Robinhood Chain")
        if len(self.minted) >= cfg.target_mints:
            self.log("already_done", minted=len(self.minted))
            return self.summary("already done")
        self.log("start", address=self.address, live=cfg.live, target_mints=cfg.target_mints,
                 max_price_eth=cfg.max_price_wei / 1e18, max_spend_eth=cfg.max_spend_wei / 1e18,
                 balance_eth=self.chain.balance(self.address) / 1e18)
        self.start_agent()
        last_status = time.time()
        try:
            while self.stop_reason is None:
                if time.time() - self.started > cfg.max_hours * 3600:
                    self.stop("MAX_HOURS reached")
                    break
                try:
                    snap = self.chain.snapshot(self.address)
                except Exception as exc:  # noqa: BLE001 - transient RPC trouble
                    self.log("rpc_error", error=str(exc)[:200])
                    time.sleep(1)
                    continue
                if snap["total_minted"] >= chain.SUPPLY:
                    self.stop("collection fully minted")
                    break
                if snap["price_wei"] > cfg.max_price_wei:
                    self.stop(f"mint price {snap['price_wei'] / 1e18:.4f} ETH above MAX_PRICE_ETH")
                    break
                self.refresh_job(snap)
                deadline = time.time() + cfg.poll_ms / 1000
                while self.stop_reason is None:
                    timeout = deadline - time.time()
                    if timeout <= 0:
                        break
                    try:
                        ev = self.events.get(timeout=timeout)
                    except queue.Empty:
                        break
                    self.handle_event(ev)
                if len(self.minted) >= cfg.target_mints:
                    self.stop("target mints reached")
                if time.time() - last_status >= cfg.stats_seconds:
                    last_status = time.time()
                    self.status(snap)
        finally:
            self.stop_agent()
        return self.summary(self.stop_reason)

    def handle_event(self, ev):
        kind = ev.get("type")
        if kind == "candidate":
            self.handle_candidate(ev)
        elif kind == "stats":
            self.hashrate = ev.get("hashrate", [])
        elif kind == "ready":
            self.log("agent_ready", workers=ev.get("workers"))
        elif kind == "worker_error":
            self.log("worker_error", worker=ev.get("worker"), text=ev.get("text"))
            self.stop("worker error")
        elif kind in ("stopped", "agent_eof"):
            self.stop("agent exited")
        elif kind == "agent_noise":
            self.log("agent_noise", text=ev.get("text"))

    def summary(self, reason):
        result = {"reason": reason, "minted": len(self.minted), "spend_eth": self.spend_wei / 1e18,
                  "reverts": self.reverts, "live": self.cfg.live}
        self.log("done", **result)
        return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--live", action="store_true", help="sign and broadcast (spends ETH)")
    p.add_argument("--local-cpu", action="store_true", help="run the CPU worker on this machine instead of SSH")
    p.add_argument("--worker", default=None, help="worker binary for --local-cpu")
    p.add_argument("--gpus", type=int, default=None)
    p.add_argument("--batch-log2", type=int, default=None)
    p.add_argument("--target-mints", type=int, default=None)
    p.add_argument("--max-price-eth", type=float, default=None)
    p.add_argument("--max-spend-eth", type=float, default=None)
    p.add_argument("--max-hours", type=float, default=None)
    p.add_argument("--state-dir", default=None)
    args = p.parse_args(argv)
    cfg = Config(live=args.live, local_cpu=args.local_cpu, worker=args.worker, gpus=args.gpus,
                 batch_log2=args.batch_log2, target_mints=args.target_mints, max_price_eth=args.max_price_eth,
                 max_spend_eth=args.max_spend_eth, max_hours=args.max_hours, state_dir=args.state_dir)
    if cfg.max_price_wei <= 0:
        raise SystemExit("set MAX_PRICE_ETH (or --max-price-eth) to the entry price you accept, e.g. 0.01")
    account = load_account(cfg)
    result = Coordinator(cfg, account).run()
    return 0 if result["reason"] in ("target mints reached", "already done") else 1


if __name__ == "__main__":
    sys.exit(main())
