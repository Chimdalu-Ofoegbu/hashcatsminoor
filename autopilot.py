#!/usr/bin/env python3
"""One command from "is it worth it right now?" to "cats minted, rental destroyed".

    python autopilot.py --dry-run          # gate only: the live numbers and the box it would rent
    python autopilot.py --rehearse --yes   # rent, build, self-test, run everything except signing
    python autopilot.py --yes              # the real thing, inside MAX_PRICE_ETH / MAX_SPEND_ETH / MAX_HOURS

Providers (PROVIDER or --provider):
    vast    rent the cheapest matching box through the vastai CLI, destroy it at the end
    ssh     a box you already have at SSH_TARGET (RunPod, a friend's rig, ...)
    local   the GPU is in this machine

The run stops, and the rental is destroyed, on any failed check: the live
target says it does not pay, the self-test fails, the coordinator hits a limit,
an exception, Ctrl-C, or the watchdog firing after MAX_HOURS plus a margin.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import requests

import chain
import coordinator
import remote as remote_mod
import vast as vast_mod
import worth_it

ROOT = Path(__file__).resolve().parent


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def fetch_eth_usd(timeout: float = 10) -> float:
    reply = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=timeout).json()
    return float(reply["ethereum"]["usd"])


OPENSEA_SLUG = "hash-cats"


def fetch_top_offer_eth(api_key: str, slug: str = OPENSEA_SLUG, timeout: float = 10) -> float:
    """Best live collection offer on OpenSea in ETH-equivalent, or 0.0 if none is listed."""
    reply = requests.get(f"https://api.opensea.io/api/v2/offers/collection/{slug}",
                         headers={"accept": "application/json", "x-api-key": api_key}, timeout=timeout).json()
    best = 0
    for offer in reply.get("offers", []):
        price = offer.get("price") or {}
        if price.get("currency", "").upper() in ("WETH", "ETH"):
            best = max(best, int(price.get("value", 0)))
    return best / 1e18


class Abort(Exception):
    pass


class Autopilot:
    def __init__(self, args):
        self.args = args
        self.provider = args.provider or env("PROVIDER", "vast")
        self.state_dir = Path(args.state_dir or env("STATE_DIR", "state"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.state_dir / "autopilot.log"
        self.gpu = (args.gpu or env("VAST_GPU", "RTX_4090")).replace(" ", "_")
        self.num_gpus = int(args.num_gpus or env("VAST_NUM_GPUS", "1"))
        self.max_dph = float(args.max_dph or env("VAST_MAX_DPH", "0.60"))
        self.image = env("VAST_IMAGE", vast_mod.DEFAULT_IMAGE)
        self.disk = int(env("VAST_DISK", "20"))
        default_cuda = "12.8" if "5090" in self.gpu or "5080" in self.gpu else "12.2"
        self.min_cuda = float(env("VAST_MIN_CUDA", default_cuda))
        self.query_extra = env("VAST_QUERY_EXTRA", "")
        self.label = env("VAST_LABEL", "hashcats")
        self.ssh_key_file = os.path.expanduser(env("SSH_KEY_FILE", "~/.ssh/id_ed25519"))
        self.remote_dir = env("REMOTE_DIR", "~/hashcatsminoor")
        if self.provider == "local":
            self.remote_dir = str(ROOT)
        self.usd_per_hour = float(env("USD_PER_HOUR", "0"))
        self.sale_eth = float(env("SALE_ETH", "0.0693"))
        self.fee_pct = float(env("FEE_PCT", "5.5"))
        self.min_profit_per_hour = float(env("MIN_PROFIT_PER_HOUR", "5"))
        self.min_remaining = int(env("MIN_REMAINING", "300"))
        self.max_hours = float(args.max_hours or env("MAX_HOURS", "2"))
        self.max_price_eth = float(args.max_price_eth or env("MAX_PRICE_ETH", "0"))
        self.target_mints = int(args.target_mints or env("TARGET_MINTS", "3"))
        self.max_spend_eth = float(env("MAX_SPEND_ETH", "0.05"))
        self.key_file = env("WALLET_KEY_FILE", "secrets/wallet.key")
        self.miner_address = env("MINER_ADDRESS")
        self.eth_usd = float(env("ETH_USD", "0")) or None
        self.vast = vast_mod.VastCLI(dry_run=False, log=self.say)
        self.chain = chain.Chain()
        self.instance_id = None
        self.instance_dph = None
        self.instance_started = None
        self.watchdog = None
        self.summary = {"provider": self.provider, "reason": None, "minted": 0, "spend_eth": 0.0,
                        "rental_usd": 0.0, "rental_hours": 0.0, "ghs": None, "instance_id": None}

    # ---- helpers ----
    def say(self, text):
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        print(line, flush=True)
        with self.log_file.open("a") as f:
            f.write(line + "\n")

    def confirm(self, prompt):
        if self.args.yes:
            return
        if not sys.stdin.isatty():
            raise Abort("confirmation needed but no terminal; pass --yes for unattended runs")
        answer = input(f"{prompt} Type RENT to continue: ").strip()
        if answer != "RENT":
            raise Abort("not confirmed")

    def address(self):
        key = Path(self.key_file)
        if key.exists():
            from eth_account import Account
            return Account.from_key(key.read_text().strip().splitlines()[0]).address
        if self.miner_address:
            return self.miner_address
        raise Abort(f"no wallet: run scripts/make_wallet.py or set MINER_ADDRESS ({self.key_file} not found)")

    # ---- steps ----
    def preflight(self):
        if self.max_price_eth <= 0:
            raise Abort("set MAX_PRICE_ETH, the entry price you accept per cat (0.01 today)")
        if self.chain.chain_id() != chain.CHAIN_ID:
            raise Abort("RPC_URL is not Robinhood Chain")
        addr = self.address()
        balance = self.chain.balance(addr) / 1e18
        need = self.max_price_eth * self.target_mints + 0.001
        self.say(f"wallet {addr} balance {balance:.4f} ETH on Robinhood Chain (need about {need:.4f})")
        if not self.args.rehearse and not self.args.dry_run and balance < need:
            raise Abort(f"fund the wallet first: {balance:.4f} ETH < {need:.4f} ETH")
        if self.eth_usd is None:
            self.eth_usd = fetch_eth_usd()
        api_key = env("OPENSEA_API_KEY")
        if api_key:
            try:
                live = fetch_top_offer_eth(api_key)
            except Exception as exc:  # noqa: BLE001 - fall back to the configured price
                live = 0.0
                self.say(f"OpenSea offer lookup failed ({type(exc).__name__}); using SALE_ETH={self.sale_eth}")
            if live > 0:
                self.say(f"OpenSea top collection offer {live:.4f} ETH (configured SALE_ETH was {self.sale_eth})")
                self.sale_eth = live
            else:
                self.say(f"no OpenSea collection offer found; using SALE_ETH={self.sale_eth}")
        self.say(f"ETH {self.eth_usd:.0f} USD, selling into {self.sale_eth} ETH after {self.fee_pct}% fees")
        if self.provider == "vast":
            if not self.vast.available():
                raise Abort("vastai CLI not found: pip install vastai && vastai set api-key <KEY>")
            user = self.vast.user()
            credit = float(user.get("credit", user.get("balance", 0)) or 0)
            self.say(f"vast.ai account {user.get('email', '?')} credit ${credit:.2f}")
            if credit < self.max_dph * self.max_hours * 1.5:
                raise Abort(f"vast.ai credit ${credit:.2f} is below {self.max_hours}h at ${self.max_dph}/h with margin")
            if not Path(self.ssh_key_file).exists():
                raise Abort(f"SSH_KEY_FILE {self.ssh_key_file} not found (the key registered with vast.ai)")
        elif self.provider == "ssh" and not env("SSH_TARGET"):
            raise Abort("PROVIDER=ssh needs SSH_TARGET=user@host")
        return addr

    def gate(self, ghs, usd_per_hour, label):
        snap = worth_it.read_snapshot(self.chain, self.address())
        econ = worth_it.economics(snap, ghs, usd_per_hour, self.eth_usd, self.sale_eth, self.fee_pct)
        self.say(f"== gate: {label} ({ghs:.2f} GH/s at ${usd_per_hour:.2f}/h) ==")
        worth_it.print_report(snap, econ, SimpleNamespace(ghs=ghs, usd_per_hour=usd_per_hour, fee_pct=self.fee_pct))
        problems = []
        if econ["remaining"] < self.min_remaining:
            problems.append(f"only {econ['remaining']} cats left (MIN_REMAINING {self.min_remaining})")
        if econ["margin_usd"] <= 0:
            problems.append("negative margin per cat")
        if econ["profit_per_hour"] < self.min_profit_per_hour:
            problems.append(f"profit ${econ['profit_per_hour']:.2f}/h below MIN_PROFIT_PER_HOUR {self.min_profit_per_hour}")
        if snap["price_wei"] > self.max_price_eth * 1e18:
            problems.append(f"entry price {snap['price_wei'] / 1e18:.4f} ETH above MAX_PRICE_ETH")
        if problems:
            raise Abort("gate failed: " + "; ".join(problems))
        self.say("gate passed")
        return econ

    def provision(self):
        if self.provider == "local":
            return remote_mod.LocalRemote(ROOT, log=self.say)
        if self.provider == "ssh":
            target = env("SSH_TARGET")
            user, _, host = target.rpartition("@")
            r = remote_mod.SshRemote(host, int(env("SSH_PORT", "22")), user or "root", env("SSH_KEY_FILE"),
                                     env("SSH_OPTS", ""), log=self.say)
            r.wait_for_ssh(timeout=120)
            return r
        offers = self.vast.search(self.gpu, self.num_gpus, self.max_dph, self.min_cuda, disk=self.disk,
                                  extra=self.query_extra)
        if not offers:
            raise Abort(f"no vast.ai offers for {self.num_gpus}x {self.gpu} under ${self.max_dph}/h; raise VAST_MAX_DPH")
        offer = offers[0]
        self.say(f"cheapest offer {offer['id']}: {offer['num_gpus']}x {offer['gpu_name']} ${offer['dph']:.3f}/h "
                 f"cuda {offer['cuda']} reliability {offer['reliability']} in {offer['geolocation']}")
        if self.args.dry_run:
            return None
        self.confirm(f"Rent it for up to {self.max_hours}h (about ${offer['dph'] * self.max_hours:.2f})?")
        self.instance_id = self.vast.create(offer["id"], self.image, self.disk, self.label)
        self.instance_started = time.time()
        self.summary["instance_id"] = self.instance_id
        self.say(f"instance {self.instance_id} created; waiting for it to boot")
        info = self.vast.wait_running(self.instance_id)
        self.instance_dph = float(info.get("dph_total") or offer["dph"])
        user, host, port = self.vast.ssh_endpoint(self.instance_id)
        r = remote_mod.SshRemote(host, port, user, self.ssh_key_file, log=self.say)
        r.wait_for_ssh()
        return r

    def setup(self, remote):
        remote.upload_repo(ROOT, self.remote_dir)
        where = remote_mod.shell_path(self.remote_dir)
        if self.args.cpu:
            cmd = (f"cd {where} && make -C miner cpu_worker && python3 scripts/gpu_selftest.py --cpu "
                   f"--worker miner/cpu_worker --seconds 1 --batch-log2 14")
        else:
            cmd = f"cd {where} && bash scripts/remote_setup.sh"
        rc, out = remote.run(cmd, timeout=1800)
        for line in out.strip().splitlines()[-25:]:
            self.say("  | " + line)
        m = re.search(r"total ([0-9.]+) GH/s", out)
        if rc != 0 or "FAILED" in out or not m:
            raise Abort("setup or self-test failed on the box; see the lines above")
        ghs = float(m.group(1))
        if ghs <= 0:
            raise Abort("self-test measured zero hashrate")
        self.summary["ghs"] = ghs
        return ghs

    def mine(self, remote, ghs):
        kwargs = remote.coordinator_kwargs(cpu=self.args.cpu)
        cfg = coordinator.Config(live=not self.args.rehearse, cpu=self.args.cpu, gpus=int(env("GPUS", "0")),
                                 remote_dir=self.remote_dir, target_mints=self.target_mints,
                                 max_price_eth=self.max_price_eth, max_spend_eth=self.max_spend_eth,
                                 max_hours=self.max_hours, state_dir=self.state_dir, key_file=self.key_file,
                                 miner_address=self.address(), **kwargs)
        account = coordinator.load_account(cfg)
        result = coordinator.Coordinator(cfg, account).run()
        self.summary.update(reason=result["reason"], minted=result["minted"], spend_eth=result["spend_eth"])
        return result

    def teardown(self):
        if self.instance_id is None:
            return
        hours = (time.time() - self.instance_started) / 3600
        self.summary["rental_hours"] = round(hours, 3)
        self.summary["rental_usd"] = round(hours * (self.instance_dph or self.max_dph), 3)
        for attempt in range(3):
            try:
                self.vast.destroy(self.instance_id)
                self.say(f"instance {self.instance_id} destroyed after {hours:.2f}h")
                self.instance_id = None
                return
            except Exception as exc:  # noqa: BLE001
                self.say(f"destroy attempt {attempt + 1} failed: {exc}")
                time.sleep(10)
        self.say(f"!!! COULD NOT DESTROY INSTANCE {self.instance_id}: run `vastai destroy instance {self.instance_id}` NOW")

    def start_watchdog(self):
        if self.provider != "vast":
            return
        fuse = self.max_hours * 3600 + 15 * 60

        def fire():
            self.say("watchdog: MAX_HOURS plus margin elapsed; destroying the rental and exiting")
            self.teardown()
            os._exit(3)

        self.watchdog = threading.Timer(fuse, fire)
        self.watchdog.daemon = True
        self.watchdog.start()

    def run(self):
        remote = None
        try:
            self.preflight()
            assumed = float(env("ASSUMED_GHS", "0")) or vast_mod.GPU_GHS.get(self.gpu, 4.0) * self.num_gpus
            rate = self.max_dph if self.provider == "vast" else self.usd_per_hour
            self.gate(assumed, rate, "before renting, assumed hashrate")
            if self.args.dry_run:
                self.provision()  # for vast this only searches and prints the offer
                self.summary["reason"] = "dry run: gate passed, nothing rented"
                return self.summary
            self.start_watchdog()
            remote = self.provision()
            ghs = self.setup(remote)
            rate = self.instance_dph if self.provider == "vast" else self.usd_per_hour
            self.gate(ghs, rate, "measured hashrate")
            self.mine(remote, ghs)
        except Abort as exc:
            self.summary["reason"] = str(exc)
            self.say(f"stopped: {exc}")
        except KeyboardInterrupt:
            self.summary["reason"] = "interrupted"
            self.say("interrupted")
        except Exception as exc:  # noqa: BLE001 - record it, tear down, exit non-zero
            self.summary["reason"] = f"error: {type(exc).__name__}: {exc}"
            self.say(traceback.format_exc().rstrip())
        finally:
            if self.watchdog:
                self.watchdog.cancel()
            self.teardown()
            out = self.state_dir / f"autopilot-{time.strftime('%Y%m%d-%H%M%S')}.json"
            out.write_text(json.dumps(self.summary, indent=2))
            self.say(f"summary: minted {self.summary['minted']}, spent {self.summary['spend_eth']:.4f} ETH, "
                     f"rental ${self.summary['rental_usd']:.2f} over {self.summary['rental_hours']:.2f}h "
                     f"-> {out}")
        return self.summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=["vast", "ssh", "local"], default=None)
    p.add_argument("--dry-run", action="store_true", help="run the gate and show the offer; rent nothing")
    p.add_argument("--rehearse", action="store_true", help="rent and mine without signing (costs rental only)")
    p.add_argument("--cpu", action="store_true", help="use the CPU worker (rehearsal on a box without a GPU)")
    p.add_argument("--yes", action="store_true", help="do not ask before renting")
    p.add_argument("--gpu", default=None, help="vast.ai GPU name, e.g. RTX_4090 or RTX_5090")
    p.add_argument("--num-gpus", type=int, default=None)
    p.add_argument("--max-dph", type=float, default=None, help="max $/hour for the rental")
    p.add_argument("--target-mints", type=int, default=None)
    p.add_argument("--max-hours", type=float, default=None)
    p.add_argument("--max-price-eth", type=float, default=None)
    p.add_argument("--state-dir", default=None)
    args = p.parse_args(argv)
    summary = Autopilot(args).run()
    reason = summary["reason"] or ""
    ok = reason in ("target mints reached", "already done") or reason.startswith("dry run")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
