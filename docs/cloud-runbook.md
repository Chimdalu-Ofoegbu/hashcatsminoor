# Cloud mining runbook

Mine Hashcats on a rented NVIDIA box while the wallet key stays on your own
machine. Two processes: `coordinator.py` here, `remote_agent.py` there.

```
your machine                                 rented GPU host
------------                                 ---------------
worth_it.py     reads the contract
coordinator.py  polls chain, verifies,  ssh  remote_agent.py  one worker per GPU
                signs, submits  ------------> miner/worker    keccak search
secrets/wallet.key never leaves            <-- candidates, hashrate
```

## 0. Gate: is it worth it right now?

```sh
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python worth_it.py --address 0xYourMiningAddress --ghs 26 --usd-per-hour 2.40 \
    --eth-usd 2500 --sale-eth 0.0693 --fee-pct 5.5
python worth_it.py --address 0xYourMiningAddress --ghs 26 --watch 30 --csv trend.csv
```

Watch for ten minutes. You want a positive `profit $/h`, a stable or falling
`bits` value, and hundreds of cats left. Rent nothing on a MARGINAL or NOT WORTH
IT verdict. The pace rule caps the whole network near one cat per 10 seconds,
so your yield is your share of network hashrate; the watch line shows both.

## 1. Wallet and funds

Make a dedicated key. Never reuse a wallet that holds anything else.

```sh
mkdir -p secrets && chmod 700 secrets
python -c "from eth_account import Account; a = Account.create(); print(a.address); open('secrets/wallet.key','w').write(a.key.hex()+'\n')"
chmod 600 secrets/wallet.key
```

Bridge ETH to Robinhood Chain (chain id 4663, gas is ETH) with the canonical
bridge from the Robinhood Chain docs, or a third-party fast bridge. Fund about
0.06 ETH for five mints at 0.01 ETH plus gas. Withdrawing back through the
canonical bridge can take days; plan the exit before you start.

## 2. Rent the box

Keccak is integer work, so consumer cards win on hashes per dollar.

| Card | Keccak rate | Typical rent | Build flag |
|---|---|---|---|
| RTX 5090 | 6.3 to 6.6 GH/s reported by another miner | $0.50 to $1.00/h | `ARCH=120`, CUDA 12.8+ |
| RTX 4090 | about 4.5 to 5 GH/s, estimated | $0.14 to $0.69/h | `ARCH=89` |
| RTX 3090 | about 2 to 3 GH/s, estimated | $0.10 to $0.25/h | `ARCH=86` |
| H100 | unmeasured | $1.49 to $6.98/h | poor value, skip |

- **Vast.ai**: cheapest. Filter by GPU model, on-demand (not interruptible),
  SSH enabled, image `nvidia/cuda:12.8.1-devel-ubuntu22.04` or any template whose
  image name contains `devel` so `nvcc` exists. Hosts are third parties: nothing
  secret goes on the box, which this design already guarantees.
- **RunPod**: Community Cloud is a bit dearer and more reliable; Secure Cloud
  if you want a datacenter host. Pick a `devel` CUDA template and enable SSH.

Any GPU count works; the agent starts one worker per GPU it sees.

## 3. On the box

```sh
apt-get update && apt-get install -y git build-essential python3   # if missing
git clone <this repo> ~/hashcatsminoor && cd ~/hashcatsminoor
bash scripts/remote_setup.sh          # builds miner/worker, runs the self-test, prints GH/s
```

The self-test compares the worker against a pure-Python Keccak, checks a real
search, and benchmarks each GPU. Do not mine with a build that fails it. If
compute-capability detection fails, run `make -C miner worker ARCH=120` yourself.

## 4. On your machine

```sh
cp .env.example .env    # edit MINER_ADDRESS, SSH_TARGET, SSH_PORT, SSH_OPTS, limits
set -a; . ./.env; set +a
ssh -p "$SSH_PORT" $SSH_OPTS "$SSH_TARGET" nvidia-smi -L       # confirm key-based SSH works
python coordinator.py                    # dry run: real GPUs, real chain, no signing
python coordinator.py --live             # spends ETH
```

The dry run logs `would_submit` with a verified nonce and stops after
`TARGET_MINTS` of them. Live mode signs `mine(nonce, anchorBlock)` with the
mint price as value and stops on: target reached, price above `MAX_PRICE_ETH`,
`MAX_SPEND_ETH` reached, `MAX_HOURS` reached, low balance, too many reverts, a
worker error, or the collection minting out. Re-run `worth_it.py` after every
few cats; the target can step up 4x every 8 cats.

`state/receipts.json` lists what actually minted. `state/pending.json` left
behind means a transaction was broadcast and its receipt never came back:
look it up on robinhoodchain.blockscout.com before starting again, then delete
the file.

## 5. Stop the meter

Destroy the instance the moment you stop. On Vast.ai: `vastai destroy instance <id>`.
A rented box idles at full price.

## 6. Sell or burn

List on OpenSea (Robinhood Chain is supported) or accept the collection offer
for instant WETH. Burning a cat pays 1,000 $HASH only in its own epoch and
halves every epoch after; only better than selling if 1,000 $HASH is worth more
than the offer. Check the $HASH price before choosing.

## Troubleshooting

- `nvcc not found`: wrong image; pick a CUDA devel template.
- `no kernel image is available`: wrong `ARCH`; rebuild for the card's compute capability.
- `start must have its low 64 bits clear`: a job line was hand-crafted wrong; the agent never does this.
- `reverted`: usually the anchor window moved between finding and landing the cat. A few are normal; `MAX_REVERTS` stops a run that keeps losing.
- `stale_candidate`: someone else minted first. Normal in a busy network.
- `rpc_error` repeating: the public RPC is rate limiting; raise `POLL_MS`.
