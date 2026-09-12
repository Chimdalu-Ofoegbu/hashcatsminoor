import chain
from fake_chain import FakeChain


def test_snapshot_roundtrip(monkeypatch):
    fake = FakeChain(target=1 << 210, price_wei=10**16, total=9000, prev=7, anchor=9, anchor_block=123)
    monkeypatch.setattr(chain, "rpc", fake.rpc)
    c = chain.Chain("http://fake")
    snap = c.snapshot("0x" + "ab" * 20)
    assert snap == {"prev": 7, "anchor": 9, "anchor_block": 123, "target": 1 << 210,
                    "price_wei": 10**16, "total_minted": 9000}
    assert c.chain_id() == 4663
    assert c.prev_work() == 7
    assert c.mint_price() == 10**16
    assert c.total_minted() == 9000
    assert c.target_for("0x" + "ab" * 20) == 1 << 210


def test_calldata_shape():
    data = chain.snapshot_calldata(chain.COLLECTION, "0x" + "ab" * 20)
    assert data[:4] == bytes.fromhex("82ad56cb")
    assert data.count(bytes.fromhex(chain.COLLECTION[2:].lower())) == 5


def test_minted_token_ids():
    me = "0x" + "ab" * 20
    receipt = {"logs": [
        {"address": chain.COLLECTION, "topics": [chain.TRANSFER_TOPIC, "0x" + "0" * 64,
                                                 "0x" + ("ab" * 20).rjust(64, "0"), "0x" + f"{42:064x}"]},
        {"address": chain.COLLECTION, "topics": [chain.TRANSFER_TOPIC, "0x" + "0" * 64,
                                                 "0x" + ("cd" * 20).rjust(64, "0"), "0x" + f"{43:064x}"]},
        {"address": "0x" + "ee" * 20, "topics": [chain.TRANSFER_TOPIC, "0x" + "0" * 64,
                                                  "0x" + ("ab" * 20).rjust(64, "0"), "0x" + f"{44:064x}"]},
    ]}
    assert chain.minted_token_ids(receipt, chain.COLLECTION, me) == [42]
