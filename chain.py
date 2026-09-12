"""Robinhood Chain RPC helpers for the Hashcats contract.

Everything goes through the module-level `rpc` function so tests can swap it.
"""
from __future__ import annotations

import os

import requests
from eth_abi import decode, encode
from eth_utils import keccak

COLLECTION = os.environ.get("COLLECTION_ADDRESS", "0xCA75DF55Cc9C476DB27a7375D1fc8E794cf80721")
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CHAIN_ID = 4663
SUPPLY = 16384
TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()

SNAPSHOT_READS = (
    ("prevWork()", [], []),
    ("currentAnchor()", [], []),
    ("targetFor(address)", ["address"], None),  # args filled with the miner address
    ("mintPrice()", [], []),
    ("totalMinted()", [], []),
)


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def rpc(url: str, method: str, params: list, timeout: float = 15.0):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    reply = requests.post(url, json=body, timeout=timeout).json()
    if "error" in reply:
        raise RuntimeError(f"RPC {method} failed: {reply['error'].get('message', reply['error'])}")
    return reply["result"]


def snapshot_calldata(collection: str, address: str) -> bytes:
    calls = []
    for sig, types, args in SNAPSHOT_READS:
        if args is None:
            args = [address]
        calls.append((collection, False, selector(sig) + encode(types, args)))
    return selector("aggregate3((address,bool,bytes)[])") + encode(["(address,bool,bytes)[]"], [calls])


def parse_snapshot(raw_hex: str) -> dict:
    out = decode(["(bool,bytes)[]"], bytes.fromhex(raw_hex.removeprefix("0x")))[0]
    if not all(ok for ok, _ in out):
        raise RuntimeError("a contract read reverted")
    anchor_block, anchor = decode(["uint256", "bytes32"], out[1][1])
    return {
        "prev": int.from_bytes(out[0][1], "big"),
        "anchor": int.from_bytes(anchor, "big"),
        "anchor_block": anchor_block,
        "target": int.from_bytes(out[2][1], "big"),
        "price_wei": int.from_bytes(out[3][1], "big"),
        "total_minted": int.from_bytes(out[4][1], "big"),
    }


class Chain:
    def __init__(self, url: str = RPC_URL, collection: str = COLLECTION):
        self.url = url
        self.collection = collection

    def call(self, method, params):
        return rpc(self.url, method, params)

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def read(self, signature, types=(), args=()) -> bytes:
        data = selector(signature) + encode(list(types), list(args))
        raw = self.call("eth_call", [{"to": self.collection, "data": "0x" + data.hex()}, "latest"])
        return bytes.fromhex(raw.removeprefix("0x"))

    def snapshot(self, address: str) -> dict:
        data = snapshot_calldata(self.collection, address)
        raw = self.call("eth_call", [{"to": MULTICALL3, "data": "0x" + data.hex()}, "latest"])
        return parse_snapshot(raw)

    def prev_work(self) -> int:
        return int.from_bytes(self.read("prevWork()"), "big")

    def mint_price(self) -> int:
        return int.from_bytes(self.read("mintPrice()"), "big")

    def total_minted(self) -> int:
        return int.from_bytes(self.read("totalMinted()"), "big")

    def target_for(self, address: str) -> int:
        return int.from_bytes(self.read("targetFor(address)", ["address"], [address]), "big")

    def owner_of(self, token_id: int) -> str:
        return "0x" + self.read("ownerOf(uint256)", ["uint256"], [token_id])[-20:].hex()

    def gas_price(self) -> int:
        return int(self.call("eth_gasPrice", []), 16)

    def balance(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def tx_count(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "pending"]), 16)

    def send_raw(self, raw: bytes) -> str:
        return self.call("eth_sendRawTransaction", ["0x" + raw.hex()])

    def receipt(self, txhash: str):
        return self.call("eth_getTransactionReceipt", [txhash])


def minted_token_ids(receipt: dict, collection: str, to_address: str) -> list[int]:
    """Token ids transferred from the zero address to `to_address` in this receipt."""
    ids = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if (
            log.get("address", "").lower() == collection.lower()
            and len(topics) == 4
            and topics[0].lower() == TRANSFER_TOPIC
            and int(topics[1], 16) == 0
            and topics[2][-40:].lower() == to_address[2:].lower()
        ):
            ids.append(int(topics[3], 16))
    return ids
