"""An in-process stand-in for the Hashcats contract on Robinhood Chain.

Handles the JSON-RPC methods the tools use, decodes signed transactions, checks
the proof of work exactly like the real contract would, and mints on success.
"""
import rlp
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak

import chain
import pow as hpow

MINE_SELECTOR = keccak(text="mine(uint256,uint256)")[:4]


class FakeChain:
    def __init__(self, target=1 << 250, price_wei=10**16, total=12000, prev=0xABC, anchor=0xDEF,
                 anchor_block=500, gas_price=10**8, chain_id=chain.CHAIN_ID):
        self.default_target = target
        self.targets = {}
        self.price = price_wei
        self.total = total
        self.prev = prev
        self.anchor = anchor
        self.anchor_block = anchor_block
        self.gas_price = gas_price
        self.chain_id = chain_id
        self.block = anchor_block + 3
        self.balances = {}
        self.nonces = {}
        self.owners = {}
        self.receipts = {}
        self.txs = []
        self.calls = []

    def fund(self, address, wei):
        self.balances[address.lower()] = wei

    def target_for(self, address):
        return self.targets.get(address.lower(), self.default_target)

    # ---- contract reads ----
    def evaluate(self, data):
        sel, args = data[:4], data[4:]
        if sel == chain.selector("prevWork()"):
            return self.prev.to_bytes(32, "big")
        if sel == chain.selector("currentAnchor()"):
            return encode(["uint256", "bytes32"], [self.anchor_block, self.anchor.to_bytes(32, "big")])
        if sel == chain.selector("targetFor(address)"):
            (address,) = decode(["address"], args)
            return self.target_for(address).to_bytes(32, "big")
        if sel == chain.selector("mintPrice()"):
            return self.price.to_bytes(32, "big")
        if sel == chain.selector("totalMinted()"):
            return self.total.to_bytes(32, "big")
        if sel == chain.selector("ownerOf(uint256)"):
            (token_id,) = decode(["uint256"], args)
            owner = self.owners.get(token_id)
            if owner is None:
                raise RuntimeError("execution reverted: nonexistent token")
            return encode(["address"], [owner])
        raise RuntimeError("unknown selector")

    def eth_call(self, tx):
        data = bytes.fromhex(tx["data"].removeprefix("0x"))
        if tx["to"].lower() == chain.MULTICALL3.lower():
            assert data[:4] == chain.selector("aggregate3((address,bool,bytes)[])")
            (calls,) = decode(["(address,bool,bytes)[]"], data[4:])
            results = []
            for target, allow_failure, calldata in calls:
                assert target.lower() == chain.COLLECTION.lower()
                try:
                    results.append((True, self.evaluate(calldata)))
                except RuntimeError:
                    if not allow_failure:
                        raise
                    results.append((False, b""))
            return "0x" + encode(["(bool,bytes)[]"], [results]).hex()
        assert tx["to"].lower() == chain.COLLECTION.lower()
        return "0x" + self.evaluate(data).hex()

    # ---- transactions ----
    def send_raw(self, raw_hex):
        raw = bytes.fromhex(raw_hex.removeprefix("0x"))
        sender = Account.recover_transaction(raw)
        fields = rlp.decode(raw)
        nonce, gas_price, gas, to, value, data = [int.from_bytes(f, "big") if i != 3 and i != 5 else f
                                                   for i, f in enumerate(fields[:6])]
        txhash = "0x" + keccak(raw).hex()
        self.txs.append({"sender": sender, "nonce": nonce, "value": value, "to": to, "data": data})
        assert nonce == self.nonces.get(sender.lower(), 0), "bad account nonce"
        self.nonces[sender.lower()] = nonce + 1
        gas_used = 150_000
        logs = []
        status = 0
        # a plain value transfer (no calldata) to any non-collection address
        if not data and to.hex().lower() != chain.COLLECTION[2:].lower():
            recipient = "0x" + to.hex()
            transfer_gas = 21_000
            self.balances[sender.lower()] = self.balances.get(sender.lower(), 0) - value - transfer_gas * gas_price
            self.balances[recipient.lower()] = self.balances.get(recipient.lower(), 0) + value
            self.block += 1
            txhash = "0x" + keccak(raw).hex()
            self.receipts[txhash] = {"status": "0x1", "gasUsed": hex(transfer_gas),
                                     "effectiveGasPrice": hex(gas_price), "logs": [],
                                     "transactionHash": txhash, "blockNumber": hex(self.block)}
            return txhash
        ok = (
            to.hex().lower() == chain.COLLECTION[2:].lower()
            and data[:4] == MINE_SELECTOR
            and value == self.price
        )
        if ok:
            pow_nonce, anchor_block = decode(["uint256", "uint256"], data[4:])
            ok = anchor_block == self.anchor_block and hpow.verify(
                sender, pow_nonce, self.prev, self.anchor, self.target_for(sender)
            )
        if ok:
            status = 1
            token_id = self.total
            self.total += 1
            self.prev = hpow.digest_int(sender, pow_nonce, self.prev, self.anchor)
            self.owners[token_id] = sender
            logs.append({
                "address": chain.COLLECTION,
                "topics": [chain.TRANSFER_TOPIC, "0x" + "0" * 64, "0x" + sender[2:].lower().rjust(64, "0"),
                           "0x" + f"{token_id:064x}"],
            })
            # streak rule: the minter's next cat needs twice the work
            self.targets[sender.lower()] = self.target_for(sender) // 2
        self.balances[sender.lower()] = self.balances.get(sender.lower(), 0) - gas_used * gas_price - (value if status else 0)
        self.block += 1
        self.receipts[txhash] = {
            "status": hex(status), "gasUsed": hex(gas_used), "effectiveGasPrice": hex(gas_price),
            "logs": logs, "transactionHash": txhash, "blockNumber": hex(self.block),
        }
        return txhash

    # ---- JSON-RPC entry point, drop-in for chain.rpc ----
    def rpc(self, url, method, params, timeout=None):
        self.calls.append(method)
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return hex(self.block)
        if method == "eth_call":
            return self.eth_call(params[0])
        if method == "eth_gasPrice":
            return hex(self.gas_price)
        if method == "eth_getBalance":
            return hex(self.balances.get(params[0].lower(), 0))
        if method == "eth_getTransactionCount":
            return hex(self.nonces.get(params[0].lower(), 0))
        if method == "eth_sendRawTransaction":
            return self.send_raw(params[0])
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        raise RuntimeError(f"unsupported method {method}")
