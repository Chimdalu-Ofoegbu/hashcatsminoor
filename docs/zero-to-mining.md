# From zero to mining, step by step

This assumes you have never rented a GPU, never bridged ETH, and never run a
miner. It ends with one command that decides whether mining pays right now,
rents a GPU if it does, mints cats within limits you set, destroys the rental,
and writes a summary. Budget about an hour for the one-time setup, most of it
waiting on money to move.

Read the money rules first. They are the whole point.

## The money rules

1. **The wallet you mine with holds only mining money.** You create it here,
   fund it with about 0.06 ETH, and nothing else ever touches it.
2. **Every run has hard limits**: price per cat (`MAX_PRICE_ETH`), total spend
   (`MAX_SPEND_ETH`), wall clock (`MAX_HOURS`), and cats to mint
   (`TARGET_MINTS`). The tools stop at whichever comes first.
3. **Nothing is rented unless the live numbers say it pays.** The gate reads the
   contract, prices a cat at what you can actually sell it for, subtracts mint,
   gas and compute, and refuses if the profit per hour is below your floor.
4. **Rentals are destroyed automatically**, on success, on failure, on Ctrl-C,
   and by a watchdog if the run hangs. You still check once with
   `vastai show instances`; a forgotten box bills by the hour.
5. **Selling is manual.** The last step is you accepting an offer on OpenSea.

Expect a first trial to cost roughly: $1 to $3 of GPU time, 0.01 ETH per cat
minted, and cents of gas. A cat sold into today's collection offer nets about
0.065 ETH. Prices move; the gate re-reads everything on every run.

## Part 1. One-time setup on your computer

You need a Mac or Linux machine, or Windows with WSL2 (Ubuntu). Everything below
runs in a terminal.

### 1.1 Tools

```sh
python3 --version      # 3.11 or newer
git --version
ssh -V
```

Missing something? macOS: `xcode-select --install` then `brew install python@3.12`.
Ubuntu or WSL2: `sudo apt-get install -y python3 python3-venv git openssh-client build-essential`.
`build-essential` (or Xcode tools) matters: the test suite compiles a small C++ program.

### 1.2 The code

```sh
git clone https://github.com/Chimdalu-Ofoegbu/hashcatsminoor.git
cd hashcatsminoor
git checkout claude/stoic-hypatia-1s8rx8
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt vastai
python -m pytest -q
```

The last line must end in `passed`. It builds the CPU miner and runs the whole
pipeline against a fake of the contract. If it fails, stop and fix that first;
nothing after this works on a machine where that fails.

### 1.3 A vast.ai account (the GPU landlord)

1. Sign up at https://cloud.vast.ai and add credit. $20 covers many trial hours.
2. Console, top right, Account, API Keys: create a key and copy it.
3. In your terminal:

```sh
vastai set api-key PASTE_THE_KEY_HERE
vastai show user            # prints your email and credit if the key works
```

4. Give vast.ai an SSH public key so rented boxes let you in:

```sh
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
vastai create ssh-key "$(cat ~/.ssh/id_ed25519.pub)"
vastai show ssh-keys
```

Keep `~/.ssh/id_ed25519` as `SSH_KEY_FILE` in step 1.5.

### 1.4 The mining wallet and its ETH

Create the wallet. This writes the private key to `secrets/wallet.key` with
owner-only permissions and prints the address:

```sh
python scripts/make_wallet.py
```

Now get ETH onto **Robinhood Chain** for that address. Robinhood Chain is an
Ethereum Layer 2 (chain id 4663); its gas is ETH. The plain route:

1. Buy ETH on any exchange. About 0.08 ETH covers a trial plus the moves below.
2. Install MetaMask and add Robinhood Chain as a network:
   RPC `https://rpc.mainnet.chain.robinhood.com`, chain id `4663`, symbol `ETH`,
   explorer `https://robinhoodchain.blockscout.com`.
3. Withdraw the ETH from the exchange to your MetaMask address on Ethereum
   mainnet, or on any network the official bridge accepts.
4. Bridge to Robinhood Chain with the official bridge linked from
   https://docs.robinhood.com/chain/bridging/ (it is the standard Arbitrum-style
   bridge; a deposit lands in minutes). Bridge about 0.065 ETH.
5. In MetaMask on Robinhood Chain, send 0.06 ETH to the address that
   `make_wallet.py` printed.
6. Confirm on the explorer: paste the address into
   https://robinhoodchain.blockscout.com and check the balance.

Withdrawing back to Ethereum through the official bridge takes days. Money you
bridge in should be money you can leave on this chain for a while.

### 1.5 Settings

```sh
cp .env.example .env
```

Edit `.env`. The lines that matter:

| Setting | Put |
|---|---|
| `MINER_ADDRESS` | the address `make_wallet.py` printed |
| `SSH_KEY_FILE` | `~/.ssh/id_ed25519` (the key you registered with vast.ai) |
| `VAST_GPU`, `VAST_NUM_GPUS`, `VAST_MAX_DPH` | `RTX_4090`, `1`, `0.60` is a sane first trial |
| `MAX_PRICE_ETH` | `0.01`, the current entry price; raise only on purpose |
| `TARGET_MINTS`, `MAX_SPEND_ETH`, `MAX_HOURS` | `3`, `0.05`, `2` for a trial |
| `SALE_ETH` | the current top collection offer on OpenSea, not the floor |
| `OPENSEA_API_KEY` | optional; a free key from opensea.io makes the gate read the live top offer instead |
| `MIN_PROFIT_PER_HOUR` | `5` means: rent only if the numbers say $5 per hour or better |

Load it into your shell every time you open a new terminal:

```sh
set -a; . ./.env; set +a
```

## Part 2. The three commands

Run them in this order. Each one is a rehearsal for the next.

### 2.1 Look, free

```sh
python autopilot.py --dry-run
```

Reads the live contract, prices everything, prints the report, and shows the
cheapest box it would rent. It rents nothing. Read the verdict line. On
`gate failed` it tells you which rule refused: too few cats left, negative
margin, profit below your floor, or the entry price above `MAX_PRICE_ETH`.
Nothing to fix; come back later or lower your expectations deliberately.

### 2.2 Rehearse, about a dollar

```sh
python autopilot.py --rehearse --yes
```

Rents the box, uploads the miner, builds the CUDA worker for that card, runs
the self-test and benchmark, re-runs the gate with the measured hashrate, then
runs the whole mining loop **without signing anything**. You pay the rental for
a few minutes and see `would_submit` lines with verified nonces. This is the
step that proves the CUDA build, which could not be tested where this code was
written. If the self-test prints `FAILED`, stop and report the output.

### 2.3 Mine

```sh
python autopilot.py --yes
```

Same as the rehearsal, but transactions are signed on your machine and sent.
It stops at `TARGET_MINTS`, or at any limit, destroys the rental, and writes
`state/autopilot-<timestamp>.json` plus `state/receipts.json` listing every cat
that actually minted with its transaction hash. Without `--yes` it pauses once
and asks you to type `RENT` before spending.

What you will see, in order: the wallet balance, the gate report, the offer,
"instance created", the box booting (a minute or two), the build and self-test
(two to five minutes the first time), the second gate, then coordinator lines:
`job` when the network moves, `MINTED` when a cat is yours, `stale_candidate`
when someone beat you to one, and `status` every ten seconds with your hashrate.

## Part 3. Leaving it unattended

```sh
set -a; . ./.env; set +a
nohup bash scripts/autoloop.sh > state/autoloop.log 2>&1 &
```

This runs the autopilot every 30 minutes for up to 48 passes. A pass that
fails the gate costs nothing and just waits for the next slot, so the loop only
spends when the numbers are good. Stop it with `touch state/STOP`. Tune with
`LOOP_INTERVAL_MINUTES`, `LOOP_MAX_PASSES`, `LOOP_MAX_CONSECUTIVE_FAILS`.

Each pass still obeys `TARGET_MINTS` and `MAX_SPEND_ETH`, so the loop's total
exposure is passes times those limits. Keep the wallet balance as the real cap:
it cannot spend what is not there.

## Part 4. Selling

1. Open https://opensea.io, connect a wallet that can import your mining key
   (MetaMask: import account, paste the key from `secrets/wallet.key`), switch
   to Robinhood Chain, open your profile.
2. Each cat shows the collection's top offer. Accepting it pays WETH on Robinhood
   Chain immediately. Listing at the floor waits and the floor has been falling.
3. Alternative: burning a cat pays 1,000 $HASH in its own epoch, halving every
   epoch after. Only better than the offer if 1,000 $HASH trades above it.
4. Move proceeds out through the bridge when you are done, allowing for the
   withdrawal delay.

## Part 5. When something goes wrong

- **`vastai show instances` lists a box after a run**: destroy it now:
  `vastai destroy instance <ID>`. The autopilot logs "COULD NOT DESTROY" if
  this ever happens.
- **`state/pending.json` exists**: a transaction went out and its receipt never
  came back. Look the hash up on the explorer. If it minted, the cat is yours;
  if it reverted, you lost gas only. Delete the file, then run again.
- **Many `reverted` lines**: the anchor window is moving faster than your
  submissions land. `MAX_REVERTS` (default 5) stops the run. Report it.
- **`no vast.ai offers`**: raise `VAST_MAX_DPH` or pick another `VAST_GPU`.
- **`nvcc not found` or `no kernel image`** in the setup output: the image lacks
  the CUDA toolkit or the card is newer than it. Set `VAST_IMAGE` to a newer
  `nvidia/cuda:*-devel-ubuntu22.04` tag.
- **`rpc_error` repeating**: the public RPC is throttling; set `POLL_MS=400`.
- **The gate keeps failing**: that is the tool doing its job. The margin lives
  or dies on the collection offer and the network's hashrate, both of which you
  can watch with `python worth_it.py --address $MINER_ADDRESS --watch 30`.

## What was and was not tested

Tested here, offline: the Keccak core against a reference, the CPU worker, the
agent, the coordinator's signing and receipt handling against a contract fake,
the vast.ai wrapper against a fake of the CLI, and the autopilot end to end in
local and rehearsal modes. The CUDA worker was compiled for the RTX 4090
target and assembled for the RTX 5090 target with clang and NVIDIA's own
assembler (`scripts/cuda_compile_check.sh`), but it has never been launched on
a GPU, and the vast.ai commands have not been run against the live service.
The rehearsal in 2.2 is where both get proven, for about a dollar.
