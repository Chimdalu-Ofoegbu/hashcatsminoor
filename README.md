# hashcatsminoor

Go/no-go check for mining [Hashcats](https://hashcats.fun) (16,384 proof-of-work
pixel cats on Robinhood Chain, chain id 4663) on rented cloud GPUs.

`worth_it.py` reads the live contract (`0xCA75DF55Cc9C476DB27a7375D1fc8E794cf80721`)
with one Multicall3 call and prints, for the address you would mine with:

- cats minted so far and cats left
- the current entry price
- the current target as expected hashes per cat
- expected minutes per cat at your hashrate
- compute, mint, and gas cost per cat versus the resale price you expect
- a one-line verdict

It is read-only. It never signs or broadcasts anything.

## Run

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python worth_it.py --address 0xYourMiningAddress --ghs 26 --usd-per-hour 2.40 \
    --eth-usd 2500 --sale-eth 0.0693 --fee-pct 5.5
```

`--ghs` is your total hashrate. The community CUDA miner measured about 6.3 to
6.6 GH/s per RTX 5090, so a four-card box is about 26 GH/s. `--sale-eth` should
be the price you can actually sell into (the top collection offer is the honest
number), not the floor.

## How the difficulty behaves

From the official docs, four rules set the work per cat:

1. A floor of `2^(26 + epoch)` hashes so an idle network still pays something.
2. Every 8 cats the contract retargets toward one cat per 10 seconds, up to 4x
   harder or 2x easier per step.
3. Each recent mint by an address doubles that address's work; the streak cools
   one level every 10 seconds.
4. The work ramps at the end of the collection.

Rule 2 caps the whole network at roughly 8,640 cats a day, so your yield is your
share of network hashrate, not your raw hashrate. Re-run the check after every
few mints; the target moves.

## Mining itself

Do not run the browser miner on a rented box. Use the open-source
[hashcats-cuda-miner](https://github.com/asbryx/hashcats-cuda-miner): its
coordinator keeps the wallet key on your own machine and ships only a worker
script to the GPU host over SSH. Rebuild the kernel for your card
(`-arch=sm_120` for RTX 5090, `sm_89` for RTX 4090, `sm_86` for RTX 3090).

## Tests

```sh
python -m pytest -q
```

The tests mock the RPC; the script was not run against the live chain from the
environment it was written in.
