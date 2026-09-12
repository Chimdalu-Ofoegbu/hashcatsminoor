#!/usr/bin/env python3
"""Create a dedicated mining wallet: writes secrets/wallet.key (mode 600) and prints
the address to fund. Never reuse a wallet that holds anything else."""
import argparse
import os
import sys
from pathlib import Path

from eth_account import Account


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=os.environ.get("WALLET_KEY_FILE", "secrets/wallet.key"))
    p.add_argument("--force", action="store_true", help="overwrite an existing key file")
    args = p.parse_args(argv)
    path = Path(args.out)
    if path.exists() and not args.force:
        account = Account.from_key(path.read_text().strip().splitlines()[0])
        print(f"key file already exists: {path}")
        print(f"address: {account.address}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    account = Account.create()
    with open(path, "w") as f:
        os.chmod(path, 0o600)
        f.write(account.key.hex() + "\n")
    print(f"wrote {path} (keep it private; it controls the wallet)")
    print(f"address: {account.address}")
    print("next: bridge about 0.06 ETH to this address on Robinhood Chain (chain id 4663),")
    print("      then put the address in .env as MINER_ADDRESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
