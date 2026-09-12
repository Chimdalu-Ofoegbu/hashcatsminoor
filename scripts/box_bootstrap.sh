#!/usr/bin/env bash
# Run this INSIDE a rented vast.ai GPU box, in its web terminal, when you have no
# laptop. It builds the miner on this box, takes your mining wallet, and mines
# locally here (this box reaches the chain directly, so no laptop is involved).
#
# One-paste setup from the box terminal:
#   git clone https://github.com/Chimdalu-Ofoegbu/hashcatsminoor && cd hashcatsminoor && bash scripts/box_bootstrap.sh
#
# It asks two things: your mining wallet address, and its private key (paste the
# contents of the wallet.key file). Nothing else. Add an argument to change mode:
#   bash scripts/box_bootstrap.sh --dry-run    # just the verdict, mines nothing
#   bash scripts/box_bootstrap.sh --rehearse   # build + mine without signing
#   bash scripts/box_bootstrap.sh              # real: mints within the limits below
set -uo pipefail
cd "$(dirname "$0")/.."
MODE="${1:---yes}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# 1. system tools (the CUDA devel image has nvcc + g++; we add python/git)
if ! command -v python3 >/dev/null || ! command -v git >/dev/null || ! command -v make >/dev/null; then
  say "installing python3, git, build tools (one time)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip build-essential git >/dev/null
fi
command -v nvcc >/dev/null || { echo "nvcc not found: rent a box whose image name contains 'devel' (e.g. nvidia/cuda:12.8.1-devel-ubuntu22.04)"; exit 1; }

# 2. python deps
if [ ! -x .venv/bin/python ]; then
  say "installing python dependencies (one time, a minute or two)"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
PY=.venv/bin/python

# 3. wallet
mkdir -p secrets; chmod 700 secrets
if [ ! -f secrets/wallet.key ]; then
  say "your mining wallet"
  read -rp "  paste your wallet ADDRESS (0x...): " ADDR
  printf '  paste your wallet PRIVATE KEY (hidden), then Enter: '
  read -rs KEY; echo
  printf '%s\n' "$KEY" > secrets/wallet.key
  chmod 600 secrets/wallet.key
  MINER_ADDRESS="$ADDR"
fi
# derive/confirm the address from the key and make sure they match
DERIVED="$("$PY" - <<'EOF'
from eth_account import Account
print(Account.from_key(open("secrets/wallet.key").read().split()[0]).address)
EOF
)" || { echo "that private key could not be read; delete secrets/wallet.key and try again"; exit 1; }
MINER_ADDRESS="${MINER_ADDRESS:-$DERIVED}"
if [ "${MINER_ADDRESS,,}" != "${DERIVED,,}" ]; then
  echo "the address you typed ($MINER_ADDRESS) does not match the key (which is $DERIVED)."
  echo "using the key's address: $DERIVED"
  MINER_ADDRESS="$DERIVED"
fi
say "wallet $MINER_ADDRESS"

# 4. .env for local mining on this box
if [ ! -f .env ]; then
  cat > .env <<EOF
RPC_URL=https://rpc.mainnet.chain.robinhood.com
COLLECTION_ADDRESS=0xCA75DF55Cc9C476DB27a7375D1fc8E794cf80721
WALLET_KEY_FILE=secrets/wallet.key
MINER_ADDRESS=$MINER_ADDRESS
PROVIDER=local
# what this box costs you per hour on vast.ai (set it to the price you rented at,
# so the gate can tell whether mining actually pays):
USD_PER_HOUR=1.20
MAX_PRICE_ETH=0.01
TARGET_MINTS=3
MAX_SPEND_ETH=0.05
MAX_HOURS=6
SALE_ETH=0.0693
FEE_PCT=5.5
MIN_PROFIT_PER_HOUR=5
MIN_REMAINING=300
STATE_DIR=state
EOF
fi
set -a; . ./.env; set +a

# 5. build, self-test, gate, and mine locally on this box
say "starting autopilot (build -> self-test -> gate -> mine). Ctrl-C to stop."
exec "$PY" autopilot.py --provider local "$MODE"
