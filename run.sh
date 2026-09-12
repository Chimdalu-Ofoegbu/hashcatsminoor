#!/usr/bin/env bash
# One command to go from a fresh clone to mining. Re-run it as many times as you
# like: each run checks the prerequisites, does everything it can automatically,
# and stops at the FIRST thing only you can do, telling you exactly what to paste.
# When nothing is left for you, it starts the autopilot.
#
#   bash run.sh            # advance setup, then dry-run (rents nothing)
#   bash run.sh go         # once setup is done: mine one batch (spends ETH)
#   bash run.sh loop       # once setup is done: mine on a loop, unattended
set -uo pipefail
cd "$(dirname "$0")"
MODE="${1:-check}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
todo() { printf '\n\033[1;33mDO THIS:\033[0m %s\n' "$*"; exit 1; }
ok()   { printf '  \033[32mok\033[0m %s\n' "$*"; }

# 1. system tools
for tool in python3 git ssh; do
  command -v "$tool" >/dev/null || todo "install $tool first (macOS: xcode-select --install; Ubuntu/WSL: sudo apt-get install -y python3 python3-venv git openssh-client build-essential)"
done
command -v g++ >/dev/null || command -v clang++ >/dev/null || todo "install a C++ compiler (macOS: xcode-select --install; Ubuntu/WSL: sudo apt-get install -y build-essential)"
ok "python3, git, ssh, compiler"

# 2. virtualenv + deps
if [ ! -x .venv/bin/python ]; then
  say "creating .venv and installing dependencies (one time, a minute or two)"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt vastai
fi
PY=.venv/bin/python
VASTAI=.venv/bin/vastai
ok "dependencies installed"

# 3. wallet
if [ ! -f secrets/wallet.key ]; then
  say "creating your dedicated mining wallet"
  "$PY" scripts/make_wallet.py
  todo "1) copy the address printed just above into .env as MINER_ADDRESS (run: cp -n .env.example .env; then edit it)
       2) send about 0.06 ETH to that address on Robinhood Chain (see docs/zero-to-mining.md part 1.4), then re-run: bash run.sh"
fi
ADDR="$("$PY" - <<'EOF'
from eth_account import Account
print(Account.from_key(open("secrets/wallet.key").read().split()[0]).address)
EOF
)"
ok "wallet $ADDR"

# 4. .env
if [ ! -f .env ]; then
  cp .env.example .env
  todo "edit .env: set MINER_ADDRESS=$ADDR (and review MAX_PRICE_ETH / TARGET_MINTS / VAST_GPU), then re-run: bash run.sh"
fi
set -a; . ./.env; set +a
if [ "${MINER_ADDRESS:-}" != "$ADDR" ]; then
  todo "set MINER_ADDRESS=$ADDR in .env (it must match your wallet), then re-run: bash run.sh"
fi
ok ".env loaded (MAX_PRICE_ETH=${MAX_PRICE_ETH:-?} TARGET_MINTS=${TARGET_MINTS:-?} VAST_GPU=${VAST_GPU:-?})"

# 5. ssh key for rented boxes
KEY="${SSH_KEY_FILE:-secrets/vast_ed25519}"
if [ ! -f "$KEY" ]; then
  say "creating an SSH key for rented boxes ($KEY)"
  mkdir -p "$(dirname "$KEY")"; chmod 700 secrets 2>/dev/null || true
  ssh-keygen -t ed25519 -N "" -q -f "$KEY" -C hashcats-autopilot
fi
grep -q "^SSH_KEY_FILE=" .env || printf 'SSH_KEY_FILE=%s\n' "$KEY" >> .env
ok "ssh key $KEY"

# 6. vast.ai
if [ "${PROVIDER:-vast}" = "vast" ]; then
  if ! "$VASTAI" show user >/dev/null 2>&1; then
    todo "connect vast.ai (the GPU landlord):
       1) sign up at https://cloud.vast.ai and add ~\$10 credit
       2) create an API key: Account -> API Keys
       3) run: $VASTAI set api-key YOUR_KEY
       then re-run: bash run.sh"
  fi
  if ! "$VASTAI" show ssh-keys 2>/dev/null | grep -q "$(cut -d' ' -f2 "$KEY.pub")"; then
    say "registering your SSH key with vast.ai"
    "$VASTAI" create ssh-key "$(cat "$KEY.pub")" >/dev/null 2>&1 || true
  fi
  ok "vast.ai connected"
fi

# 7. balance
BAL="$("$PY" - <<EOF 2>/dev/null || echo unknown
import chain
try:
    print(f"{chain.Chain().balance('$ADDR')/1e18:.4f}")
except Exception:
    print("unknown")
EOF
)"
NEED="$("$PY" - <<EOF
print(float("${MAX_PRICE_ETH:-0.01}") * int("${TARGET_MINTS:-3}") + 0.001)
EOF
)"
if [ "$BAL" = "unknown" ]; then
  say "could not read your balance (network to Robinhood Chain may be down here); the autopilot will re-check"
elif "$PY" - <<EOF
import sys; sys.exit(0 if float("$BAL") >= float("$NEED") else 1)
EOF
then ok "balance $BAL ETH (need ~$NEED)"
else
  todo "fund the wallet: it holds $BAL ETH but needs about $NEED. Send ETH to $ADDR on Robinhood Chain, then re-run: bash run.sh"
fi

# 8. go
say "setup complete."
case "$MODE" in
  go)   exec "$PY" autopilot.py --yes ;;
  loop) exec bash scripts/autoloop.sh ;;
  *)
    "$PY" autopilot.py --dry-run || true
    printf '\nThat was a DRY RUN (nothing rented). When you are ready:\n'
    printf '  bash run.sh go     mine one batch (spends ETH, destroys the rental when done)\n'
    printf '  bash run.sh loop   keep trying every 30 min, spend only when it pays\n'
    ;;
esac
