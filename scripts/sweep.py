#!/usr/bin/env python3
"""Send ETH out of a mining wallet on Robinhood Chain. Run it where the chain is
reachable (a rented box's terminal, or any machine with internet), NOT in a
restricted sandbox.

    python scripts/sweep.py 0xDESTINATION                 # send everything minus gas
    python scripts/sweep.py 0xDESTINATION --amount 0.02   # send exactly 0.02 ETH
    python scripts/sweep.py 0xDESTINATION --key-file secrets/wallet.key --yes

It reads the wallet's balance, nonce and gas price, builds a plain transfer,
shows you the exact numbers, and (after you confirm, or with --yes) broadcasts
and waits for the receipt. A transfer is irreversible; check the destination.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address

import chain

GAS_LIMIT = 21_000  # a plain ETH transfer


def load_key(path: str) -> "Account":
    lines = [l.strip() for l in Path(path).read_text().splitlines() if l.strip()]
    if not lines:
        raise SystemExit(f"no key in {path}")
    return Account.from_key(lines[0])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("to", help="destination address (0x...)")
    p.add_argument("--key-file", default=os.environ.get("WALLET_KEY_FILE", "secrets/wallet.key"))
    p.add_argument("--rpc", default=os.environ.get("RPC_URL", chain.RPC_URL))
    p.add_argument("--amount", type=float, default=None, help="ETH to send; default is everything minus gas")
    p.add_argument("--gas-price-gwei", type=float, default=None, help="override gas price")
    p.add_argument("--yes", action="store_true", help="do not ask before broadcasting")
    args = p.parse_args(argv)

    try:
        dest = to_checksum_address(args.to)
    except Exception:
        raise SystemExit(f"'{args.to}' is not a valid address")

    account = load_key(args.key_file)
    c = chain.Chain(args.rpc)
    if c.chain_id() != chain.CHAIN_ID:
        raise SystemExit(f"RPC is not Robinhood Chain (expected chain id {chain.CHAIN_ID})")

    balance = c.balance(account.address)
    nonce = c.tx_count(account.address)
    gas_price = int(args.gas_price_gwei * 1e9) if args.gas_price_gwei else c.gas_price()
    fee = GAS_LIMIT * gas_price
    if args.amount is not None:
        value = int(args.amount * 1e18)
    else:
        value = balance - fee
    if value <= 0:
        raise SystemExit(f"nothing to send: balance {balance/1e18:.6f} ETH, gas needs {fee/1e18:.6f} ETH")
    if value + fee > balance:
        raise SystemExit(f"balance {balance/1e18:.6f} ETH cannot cover {value/1e18:.6f} + gas {fee/1e18:.6f}")

    print(f"from    {account.address}")
    print(f"to      {dest}")
    print(f"amount  {value/1e18:.6f} ETH")
    print(f"gas     {fee/1e18:.6f} ETH  (21000 x {gas_price/1e9:.4f} gwei)")
    print(f"leaves  {(balance - value - fee)/1e18:.6f} ETH in the wallet")
    if not args.yes:
        if input("send this? type YES to confirm: ").strip() != "YES":
            raise SystemExit("cancelled")

    tx = {"chainId": chain.CHAIN_ID, "nonce": nonce, "to": dest, "value": value,
          "gas": GAS_LIMIT, "gasPrice": gas_price, "data": b""}
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txhash = c.send_raw(bytes(raw))
    print(f"sent    {txhash}")
    print(f"explorer https://robinhoodchain.blockscout.com/tx/{txhash}")

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            receipt = c.receipt(txhash)
        except Exception:
            receipt = None
        if receipt:
            ok = int(receipt.get("status", "0x0"), 16) == 1
            print("confirmed" if ok else "REVERTED (funds not moved; gas spent)")
            return 0 if ok else 1
        time.sleep(2)
    print("broadcast, but no receipt yet; check the explorer link above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
