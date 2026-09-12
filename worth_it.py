#!/usr/bin/env python3
"""Read-only "is it worth it?" check for mining Hashcats on rented GPUs.

Reads the live Hashcats contract on Robinhood Chain (chain id 4663) with one
Multicall3 call, then turns the current target into expected hashes, expected
minutes on your hashrate, and a dollar margin per cat against the resale price
you expect. It never signs or sends a transaction.

Example (4x RTX 5090 rented at $2.40/hour, selling into a 0.0693 ETH offer):

    python worth_it.py --ghs 26 --usd-per-hour 2.40 --eth-usd 2500 \
        --sale-eth 0.0693 --fee-pct 5.5

Address matters: the contract exposes a per-address target (the streak rule
doubles the work after each of that address's recent mints), so pass the
address you would mine with via --address for the number that applies to you.
"""
import argparse
import os
import sys

import requests
from eth_abi import decode, encode
from eth_utils import keccak

COLLECTION = os.environ.get(
    "COLLECTION_ADDRESS", "0xCA75DF55Cc9C476DB27a7375D1fc8E794cf80721"
)
RPC = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SUPPLY = 16384
PLAN_SECONDS_PER_CAT = 10  # the contract retargets toward one cat per 10 s
GAS_LIMIT = 400_000  # conservative limit the community miner uses
CHAIN_ID = 4663


def rpc(url, method, params):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    reply = requests.post(url, json=body, timeout=15).json()
    if "error" in reply:
        raise RuntimeError(f"RPC error: {reply['error']}")
    return reply["result"]


def snapshot_calldata(address):
    specs = [
        ("prevWork()", [], []),
        ("currentAnchor()", [], []),
        ("targetFor(address)", ["address"], [address]),
        ("mintPrice()", [], []),
        ("totalMinted()", [], []),
    ]
    calls = [
        (COLLECTION, False, keccak(text=sig)[:4] + encode(types, args))
        for sig, types, args in specs
    ]
    return keccak(text="aggregate3((address,bool,bytes)[])")[:4] + encode(
        ["(address,bool,bytes)[]"], [calls]
    )


def parse_snapshot(raw_hex):
    out = decode(["(bool,bytes)[]"], bytes.fromhex(raw_hex.removeprefix("0x")))[0]
    if not all(ok for ok, _ in out):
        raise RuntimeError("one of the contract reads reverted")
    anchor_block, _anchor = decode(["uint256", "bytes32"], out[1][1])
    return {
        "prev_work": int.from_bytes(out[0][1], "big"),
        "anchor_block": anchor_block,
        "target": int.from_bytes(out[2][1], "big"),
        "price_wei": int.from_bytes(out[3][1], "big"),
        "total_minted": int.from_bytes(out[4][1], "big"),
    }


def read_chain(url, address):
    chain_id = int(rpc(url, "eth_chainId", []), 16)
    if chain_id != CHAIN_ID:
        raise RuntimeError(f"RPC is chain {chain_id}, expected {CHAIN_ID}")
    raw = rpc(
        url,
        "eth_call",
        [{"to": MULTICALL3, "data": "0x" + snapshot_calldata(address).hex()}, "latest"],
    )
    snap = parse_snapshot(raw)
    snap["gas_price_wei"] = int(rpc(url, "eth_gasPrice", []), 16)
    return snap


def economics(snap, ghs, usd_per_hour, eth_usd, sale_eth, fee_pct):
    if snap["target"] == 0:
        raise RuntimeError("target is zero: mint is closed or paused")
    expected_hashes = (1 << 256) // snap["target"]
    difficulty_bits = expected_hashes.bit_length() - 1
    hashrate = ghs * 1e9
    expected_seconds = expected_hashes / hashrate
    compute_usd = expected_seconds / 3600 * usd_per_hour
    price_eth = snap["price_wei"] / 1e18
    mint_usd = price_eth * eth_usd
    # Same gas policy as the community miner: twice the quoted price, at least 0.1 gwei.
    gas_price = max(snap["gas_price_wei"] * 2, 100_000_000)
    gas_usd = GAS_LIMIT * gas_price / 1e18 * eth_usd
    net_sale_usd = sale_eth * (1 - fee_pct / 100) * eth_usd
    margin_before_compute = net_sale_usd - mint_usd - gas_usd
    margin_usd = margin_before_compute - compute_usd
    breakeven_hours = max(margin_before_compute, 0) / usd_per_hour if usd_per_hour else 0
    affordable_hashes = int(breakeven_hours * 3600 * hashrate)
    implied_network_ghs = expected_hashes / PLAN_SECONDS_PER_CAT / 1e9
    return {
        "expected_hashes": expected_hashes,
        "difficulty_bits": difficulty_bits,
        "expected_seconds": expected_seconds,
        "compute_usd": compute_usd,
        "price_eth": price_eth,
        "mint_usd": mint_usd,
        "gas_usd": gas_usd,
        "net_sale_usd": net_sale_usd,
        "margin_usd": margin_usd,
        "breakeven_hours": breakeven_hours,
        "affordable_bits": affordable_hashes.bit_length() - 1 if affordable_hashes else 0,
        "implied_network_ghs": implied_network_ghs,
        "remaining": SUPPLY - snap["total_minted"],
    }


def verdict(econ):
    if econ["remaining"] <= 0:
        return "STOP: the collection is fully minted."
    ratio = econ["margin_usd"] / econ["net_sale_usd"] if econ["net_sale_usd"] else 0
    if econ["margin_usd"] <= 0:
        return "NOT WORTH IT: expected cost per cat exceeds what a cat sells for."
    if ratio < 0.4:
        return "MARGINAL: profit per cat is thin; a 2x difficulty step or a lower offer wipes it out."
    return "WORTH A BOUNDED TRIAL: rent for one or two hours and re-check after every mint."


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", default=os.environ.get("MINER_ADDRESS", "0x" + "0" * 40),
                   help="address you would mine with (its personal target is used)")
    p.add_argument("--rpc", default=RPC)
    p.add_argument("--ghs", type=float, default=26.0, help="your hashrate in GH/s (4x RTX 5090 is about 26)")
    p.add_argument("--usd-per-hour", type=float, default=2.40, help="what the rented box costs per hour")
    p.add_argument("--eth-usd", type=float, default=2500.0)
    p.add_argument("--sale-eth", type=float, default=0.0693, help="price you expect to sell one cat for")
    p.add_argument("--fee-pct", type=float, default=5.5, help="marketplace fee plus creator royalty")
    args = p.parse_args(argv)

    snap = read_chain(args.rpc, args.address)
    econ = economics(snap, args.ghs, args.usd_per_hour, args.eth_usd, args.sale_eth, args.fee_pct)

    print(f"minted            {snap['total_minted']} / {SUPPLY}  ({econ['remaining']} left)")
    print(f"mint price        {econ['price_eth']:.4f} ETH  (${econ['mint_usd']:.2f})")
    print(f"target bits       {256 - econ['difficulty_bits']} of 256  (~2^{econ['difficulty_bits']} hashes per cat)")
    print(f"expected time     {econ['expected_seconds']/60:.1f} min per cat at {args.ghs:.1f} GH/s")
    print(f"implied network   ~{econ['implied_network_ghs']:.0f} GH/s if the pace is on the 10 s plan")
    print(f"compute cost      ${econ['compute_usd']:.2f} per cat at ${args.usd_per_hour:.2f}/h")
    print(f"gas cost          ${econ['gas_usd']:.2f} per cat")
    print(f"net sale          ${econ['net_sale_usd']:.2f} per cat after {args.fee_pct:.1f}% fees")
    print(f"margin            ${econ['margin_usd']:.2f} per cat")
    print(f"break-even        {econ['breakeven_hours']:.1f} GPU-box hours per cat "
          f"(~2^{econ['affordable_bits']} hashes at your rate)")
    print(verdict(econ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
