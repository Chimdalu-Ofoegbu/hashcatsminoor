#!/usr/bin/env python3
"""Read-only "is it worth it?" check for mining Hashcats on rented GPUs.

Reads the live Hashcats contract on Robinhood Chain (chain id 4663) and turns the
current target into expected hashes, expected minutes on your hashrate, and a
dollar margin per cat against the resale price you expect. It never signs or
sends a transaction.

Snapshot:
    python worth_it.py --address 0xYou --ghs 26 --usd-per-hour 2.40 --eth-usd 2500 \
        --sale-eth 0.0693 --fee-pct 5.5

Watch the trend before renting (Ctrl-C to stop):
    python worth_it.py --address 0xYou --ghs 26 --watch 30 --csv trend.csv

The contract exposes a per-address target (the streak rule doubles the work after
each of that address's recent mints), so pass the address you would mine with.
"""
import argparse
import csv
import os
import sys
import time

import chain
import pow as hpow

PLAN_SECONDS_PER_CAT = 10  # the contract retargets toward one cat per 10 s
GAS_LIMIT = 400_000


def economics(snap, ghs, usd_per_hour, eth_usd, sale_eth, fee_pct, seconds_per_cat=PLAN_SECONDS_PER_CAT):
    if snap["target"] == 0:
        raise RuntimeError("target is zero: mint is closed or paused")
    if ghs <= 0:
        raise ValueError("hashrate must be positive")
    expected_hashes = hpow.expected_hashes(snap["target"])
    difficulty_bits = expected_hashes.bit_length() - 1
    hashrate = ghs * 1e9
    solo_seconds = expected_hashes / hashrate
    price_eth = snap["price_wei"] / 1e18
    mint_usd = price_eth * eth_usd
    gas_price = max(snap["gas_price_wei"] * 2, 100_000_000)  # same policy as the coordinator
    gas_usd = GAS_LIMIT * gas_price / 1e18 * eth_usd
    net_sale_usd = sale_eth * (1 - fee_pct / 100) * eth_usd
    margin_before_compute = net_sale_usd - mint_usd - gas_usd
    # Competitive view: the pace rule holds the network near `seconds_per_cat`,
    # so joining with `ghs` wins that share of a roughly fixed cat rate.
    network_ghs = expected_hashes / seconds_per_cat / 1e9
    share = ghs / (network_ghs + ghs)
    cats_per_hour = share * 3600 / seconds_per_cat
    compute_usd = usd_per_hour / cats_per_hour if cats_per_hour else float("inf")
    solo_compute_usd = solo_seconds / 3600 * usd_per_hour
    margin_usd = margin_before_compute - compute_usd
    profit_per_hour = cats_per_hour * margin_before_compute - usd_per_hour
    breakeven_hours = max(margin_before_compute, 0) / usd_per_hour if usd_per_hour else 0
    affordable_hashes = int(breakeven_hours * 3600 * hashrate)
    return {
        "expected_hashes": expected_hashes,
        "difficulty_bits": difficulty_bits,
        "solo_seconds": solo_seconds,
        "solo_compute_usd": solo_compute_usd,
        "network_ghs": network_ghs,
        "share": share,
        "cats_per_hour": cats_per_hour,
        "compute_usd": compute_usd,
        "price_eth": price_eth,
        "mint_usd": mint_usd,
        "gas_usd": gas_usd,
        "net_sale_usd": net_sale_usd,
        "margin_usd": margin_usd,
        "profit_per_hour": profit_per_hour,
        "breakeven_hours": breakeven_hours,
        "affordable_bits": affordable_hashes.bit_length() - 1 if affordable_hashes else 0,
        "remaining": chain.SUPPLY - snap["total_minted"],
    }


def verdict(econ):
    if econ["remaining"] <= 0:
        return "STOP: the collection is fully minted."
    if econ["margin_usd"] <= 0:
        return "NOT WORTH IT: expected cost per cat exceeds what a cat sells for."
    if econ["margin_usd"] / econ["net_sale_usd"] < 0.4:
        return "MARGINAL: profit per cat is thin; a 2x difficulty step or a lower offer wipes it out."
    return "WORTH A BOUNDED TRIAL: rent for one or two hours and re-check after every mint."


def read_snapshot(c, address):
    snap = c.snapshot(address)
    snap["gas_price_wei"] = c.gas_price()
    return snap


def print_report(snap, econ, args):
    print(f"minted            {snap['total_minted']} / {chain.SUPPLY}  ({econ['remaining']} left)")
    print(f"mint price        {econ['price_eth']:.4f} ETH  (${econ['mint_usd']:.2f})")
    print(f"target bits       {256 - econ['difficulty_bits']} of 256  (~2^{econ['difficulty_bits']} hashes per cat)")
    print(f"solo time         {econ['solo_seconds']/60:.1f} min per cat at {args.ghs:.1f} GH/s if nobody else mined "
          f"(${econ['solo_compute_usd']:.2f} compute per cat)")
    print(f"network           ~{econ['network_ghs']:.0f} GH/s implied by the 10 s plan; your share {econ['share']*100:.1f}%")
    print(f"expected yield    {econ['cats_per_hour']:.2f} cats per hour, ${econ['compute_usd']:.2f} compute per cat "
          f"at ${args.usd_per_hour:.2f}/h")
    print(f"gas cost          ${econ['gas_usd']:.2f} per cat")
    print(f"net sale          ${econ['net_sale_usd']:.2f} per cat after {args.fee_pct:.1f}% fees")
    print(f"margin            ${econ['margin_usd']:.2f} per cat, ${econ['profit_per_hour']:.2f} per hour")
    print(f"break-even        {econ['breakeven_hours']:.1f} GPU-box hours per cat "
          f"(~2^{econ['affordable_bits']} hashes at your rate)")
    print(verdict(econ))


def watch(c, args):
    writer = None
    handle = None
    if args.csv:
        handle = open(args.csv, "a", newline="")
        writer = csv.writer(handle)
        if handle.tell() == 0:
            writer.writerow(["time", "minted", "price_eth", "bits", "pace_cats_per_hour", "network_ghs",
                             "share", "cats_per_hour", "profit_per_hour_usd"])
    first_time = first_total = None
    n = 0
    try:
        while True:
            snap = read_snapshot(c, args.address)
            now = time.time()
            if first_time is None:
                first_time, first_total = now, snap["total_minted"]
            elapsed = now - first_time
            delta = snap["total_minted"] - first_total
            pace = delta / elapsed * 3600 if elapsed > 0 and delta > 0 else None
            seconds_per_cat = 3600 / pace if pace else PLAN_SECONDS_PER_CAT
            econ = economics(snap, args.ghs, args.usd_per_hour, args.eth_usd, args.sale_eth, args.fee_pct,
                             seconds_per_cat)
            pace_text = f"{pace:.0f}/h observed" if pace else "plan 360/h"
            print(f"{time.strftime('%H:%M:%S')} minted {snap['total_minted']} price {econ['price_eth']:.4f} "
                  f"bits 2^{econ['difficulty_bits']} pace {pace_text} network ~{econ['network_ghs']:.0f} GH/s "
                  f"share {econ['share']*100:.1f}% yield {econ['cats_per_hour']:.2f}/h "
                  f"profit ${econ['profit_per_hour']:.2f}/h", flush=True)
            if writer:
                writer.writerow([int(now), snap["total_minted"], f"{econ['price_eth']:.6f}", econ["difficulty_bits"],
                                 f"{pace:.2f}" if pace else "", f"{econ['network_ghs']:.1f}", f"{econ['share']:.4f}",
                                 f"{econ['cats_per_hour']:.3f}", f"{econ['profit_per_hour']:.2f}"])
                handle.flush()
            n += 1
            if args.iterations and n >= args.iterations:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass
    finally:
        if handle:
            handle.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", default=os.environ.get("MINER_ADDRESS", "0x" + "0" * 40),
                   help="address you would mine with (its personal target is used)")
    p.add_argument("--rpc", default=chain.RPC_URL)
    p.add_argument("--ghs", type=float, default=26.0, help="your hashrate in GH/s (4x RTX 5090 is about 26)")
    p.add_argument("--usd-per-hour", type=float, default=2.40, help="what the rented box costs per hour")
    p.add_argument("--eth-usd", type=float, default=2500.0)
    p.add_argument("--sale-eth", type=float, default=0.0693, help="price you expect to sell one cat for")
    p.add_argument("--fee-pct", type=float, default=5.5, help="marketplace fee plus creator royalty")
    p.add_argument("--watch", type=float, default=0, metavar="SECONDS", help="sample repeatedly and show the trend")
    p.add_argument("--csv", default=None, help="append watch samples to this CSV file")
    p.add_argument("--iterations", type=int, default=0, help="stop watching after this many samples")
    args = p.parse_args(argv)

    c = chain.Chain(args.rpc)
    if c.chain_id() != chain.CHAIN_ID:
        raise SystemExit(f"RPC is not Robinhood Chain (chain id {chain.CHAIN_ID})")
    if args.watch:
        watch(c, args)
        return 0
    snap = read_snapshot(c, args.address)
    econ = economics(snap, args.ghs, args.usd_per_hour, args.eth_usd, args.sale_eth, args.fee_pct)
    print_report(snap, econ, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
