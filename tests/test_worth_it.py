from eth_abi import encode

import worth_it


def fake_raw(target, price_wei, total, anchor_block=123):
    results = [
        (True, (7).to_bytes(32, "big")),
        (True, encode(["uint256", "bytes32"], [anchor_block, b"\x11" * 32])),
        (True, target.to_bytes(32, "big")),
        (True, price_wei.to_bytes(32, "big")),
        (True, total.to_bytes(32, "big")),
    ]
    return "0x" + encode(["(bool,bytes)[]"], [results]).hex()


def test_parse_snapshot_roundtrip():
    snap = worth_it.parse_snapshot(fake_raw(1 << 210, 10**16, 9000))
    assert snap["target"] == 1 << 210
    assert snap["price_wei"] == 10**16
    assert snap["total_minted"] == 9000
    assert snap["anchor_block"] == 123


def test_calldata_uses_multicall_and_five_reads():
    data = worth_it.snapshot_calldata("0x" + "ab" * 20)
    assert data[:4] == bytes.fromhex("82ad56cb")  # aggregate3 selector
    assert data.count(bytes.fromhex(worth_it.COLLECTION[2:].lower())) == 5


def test_economics_cheap_target_is_profitable():
    snap = {"target": 1 << 210, "price_wei": 10**16, "total_minted": 9000, "gas_price_wei": 10**8}
    econ = worth_it.economics(snap, ghs=26, usd_per_hour=2.4, eth_usd=2500, sale_eth=0.0693, fee_pct=5.5)
    assert econ["expected_hashes"] == 1 << 46
    assert econ["difficulty_bits"] == 46
    assert 0 < econ["compute_usd"] < 5
    assert econ["mint_usd"] == 25
    assert econ["margin_usd"] > 100
    assert "TRIAL" in worth_it.verdict(econ)


def test_economics_hard_target_is_not_worth_it():
    snap = {"target": 1 << 196, "price_wei": 10**16, "total_minted": 16000, "gas_price_wei": 10**8}
    econ = worth_it.economics(snap, ghs=26, usd_per_hour=2.4, eth_usd=2500, sale_eth=0.0693, fee_pct=5.5)
    assert econ["expected_hashes"] == 1 << 60
    assert econ["compute_usd"] > 1000
    assert econ["margin_usd"] < 0
    assert worth_it.verdict(econ).startswith("NOT WORTH IT")


def test_minted_out_stops():
    snap = {"target": 1 << 210, "price_wei": 10**16, "total_minted": 16384, "gas_price_wei": 10**8}
    econ = worth_it.economics(snap, ghs=26, usd_per_hour=2.4, eth_usd=2500, sale_eth=0.0693, fee_pct=5.5)
    assert worth_it.verdict(econ).startswith("STOP")


def test_main_with_mocked_rpc(monkeypatch, capsys):
    answers = {
        "eth_chainId": hex(4663),
        "eth_call": fake_raw(1 << 208, 10**16, 12000),
        "eth_gasPrice": hex(10**8),
    }
    monkeypatch.setattr(worth_it, "rpc", lambda url, method, params: answers[method])
    assert worth_it.main(["--ghs", "26", "--usd-per-hour", "2.4"]) == 0
    out = capsys.readouterr().out
    assert "12000 / 16384" in out
    assert "2^48 hashes per cat" in out
