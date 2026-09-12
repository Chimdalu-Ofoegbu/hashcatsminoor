# hashcatsminoor

Clean-room tooling to mine [Hashcats](https://hashcats.fun), the 16,384
proof-of-work pixel cats on Robinhood Chain (chain id 4663), on rented GPUs
without ever putting a wallet key on the rented machine.

| Piece | Runs on | What it does |
|---|---|---|
| `autopilot.py` | you | Gate on live numbers, rent a box, build and self-test, mine within limits, destroy the rental |
| `vast.py`, `remote.py` | you | vast.ai CLI wrapper; SSH upload and command runner (or this machine) |
| `scripts/make_wallet.py`, `scripts/autoloop.sh` | you | Dedicated mining wallet; unattended retry loop |
| `worth_it.py` | you | Reads the live contract and says whether renting pays, once or as a trend |
| `coordinator.py` | you | Polls the chain, feeds jobs to the GPU host, verifies candidates, signs and submits mints |
| `remote_agent.py` | GPU host | Standard-library only; one worker per GPU, reports candidates and hashrate |
| `miner/worker.cu` | GPU host | CUDA Keccak-256 search |
| `miner/cpu_worker.cpp` | anywhere | Same core and protocol on CPU, used by the tests and dry runs |
| `scripts/remote_setup.sh` | GPU host | Builds the worker for the installed card and runs the self-test |
| `scripts/gpu_selftest.py` | GPU host | Checks the worker against a pure-Python Keccak and benchmarks it |
| `pow.py`, `chain.py` | you | Proof-of-work reference and RPC helpers |

The work is `keccak256(address[20] || nonce[32] || prevWork[32] || anchor[32]) < targetFor(address)`,
submitted as `mine(nonce, anchorBlock)` with the epoch's entry price as value.
Contract: `0xCA75DF55Cc9C476DB27a7375D1fc8E794cf80721`.

## Quick start

Never done any of this? Read [docs/zero-to-mining.md](docs/zero-to-mining.md):
accounts, wallet, funding, and then three commands.

```sh
python autopilot.py --dry-run          # free: live numbers and the box it would rent
python autopilot.py --rehearse --yes   # about a dollar: rent, build, self-test, mine without signing
python autopilot.py --yes              # mint within MAX_PRICE_ETH / MAX_SPEND_ETH / MAX_HOURS, then destroy the rental
bash scripts/autoloop.sh               # unattended: retry every 30 minutes, spend only when the gate passes
```

`autopilot.py` rents through the vast.ai CLI (`PROVIDER=vast`), or uses a box
you already have (`PROVIDER=ssh`) or this machine's GPU (`PROVIDER=local`).

The manual route is in [docs/cloud-runbook.md](docs/cloud-runbook.md). In short:

```sh
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python worth_it.py --address 0xYou --ghs 26 --usd-per-hour 2.40      # gate
# on the rented box: git clone, then bash scripts/remote_setup.sh
cp .env.example .env && set -a; . ./.env; set +a
python coordinator.py            # dry run over SSH
python coordinator.py --live     # spends ETH, within MAX_PRICE_ETH / MAX_SPEND_ETH / MAX_HOURS
```

## Safety properties

- The private key is read only by `coordinator.py` on your machine. The GPU host
  receives public work (address, prevWork, anchor, target) and nothing else, not
  even the RPC URL.
- Every candidate is re-verified locally before a transaction is built, and a
  worker that produces an invalid candidate halts the run.
- Hard stops: entry-price ceiling, total spend, wall clock, revert count, and a
  pending-transaction marker that blocks restarts until you reconcile it.
- Default mode is a dry run; `--live` is explicit.

## Tests

```sh
python -m pytest -q
```

The suite builds the CPU worker with `g++` and checks it against the Python
reference for random inputs, drives the agent and the coordinator end to end
against an in-process fake of the contract (including signed transactions,
receipts, reverts and the streak rule), and validates the self-test script.

`miner/worker.cu` is compile-checked without a GPU by
`scripts/cuda_compile_check.sh`: clang's CUDA front end builds the device code
for sm_89 and the host code, and NVIDIA's `ptxas` assembles the PTX for sm_120.
What no test here can do is launch the kernel, so run `scripts/gpu_selftest.py`
on the GPU host before mining with it.

## Difficulty, from the official docs

1. A floor of `2^(26 + epoch)` hashes so an idle network still pays something.
2. Every 8 cats the contract retargets toward one cat per 10 seconds, up to 4x
   harder or 2x easier per step.
3. Each recent mint by an address doubles that address's work; the streak cools
   one level every 10 seconds.
4. The work ramps at the end of the collection.

Rule 2 caps the network near 8,640 cats a day, so yield is your share of the
network's hashrate. `worth_it.py --watch` shows the observed pace and your
expected share, cats per hour, and profit per hour.
