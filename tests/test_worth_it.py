import chain
import worth_it
from fake_chain import FakeChain

ARGS = dict(ghs=26, usd_per_hour=2.4, eth_usd=2500, sale_eth=0.0693, fee_pct=5.5)


def snap(target, total=9000, price=10**16):
    return {"target": target, "price_wei": price, "total_minted": total, "gas_price_wei": 10**8}


def test_cheap_target_is_profitable():
    econ = worth_it.economics(snap(1 << 210), **ARGS)
    assert econ["expected_hashes"] == 1 << 46
    assert econ["difficulty_bits"] == 46
    assert econ["mint_usd"] == 25
    assert round(econ["network_ghs"]) == 7037  # 2^46 hashes every 10 s
    assert 0.003 < econ["share"] < 0.004
    assert 1.2 < econ["cats_per_hour"] < 1.4
    assert 1.5 < econ["compute_usd"] < 2.0
    assert abs(econ["compute_usd"] - econ["solo_compute_usd"]) < 0.01  # equal when pace is on plan
    assert econ["margin_usd"] > 130
    assert econ["profit_per_hour"] > 150
    assert "TRIAL" in worth_it.verdict(econ)


def test_hard_target_is_not_worth_it():
    econ = worth_it.economics(snap(1 << 196, total=16000), **ARGS)
    assert econ["expected_hashes"] == 1 << 60
    assert econ["solo_compute_usd"] > 1000
    assert econ["compute_usd"] > 1000
    assert econ["margin_usd"] < 0
    assert worth_it.verdict(econ).startswith("NOT WORTH IT")


def test_minted_out_stops():
    econ = worth_it.economics(snap(1 << 210, total=16384), **ARGS)
    assert worth_it.verdict(econ).startswith("STOP")


def test_observed_pace_changes_competitive_numbers():
    fast = worth_it.economics(snap(1 << 200), seconds_per_cat=5, **ARGS)
    plan = worth_it.economics(snap(1 << 200), **ARGS)
    assert fast["network_ghs"] == 2 * plan["network_ghs"]
    assert fast["share"] < plan["share"]


def test_snapshot_report(monkeypatch, capsys):
    fake = FakeChain(target=1 << 208, price_wei=10**16, total=12000)
    monkeypatch.setattr(chain, "rpc", fake.rpc)
    assert worth_it.main(["--ghs", "26", "--usd-per-hour", "2.4"]) == 0
    out = capsys.readouterr().out
    assert "12000 / 16384" in out
    assert "2^48 hashes per cat" in out
    assert "WORTH A BOUNDED TRIAL" in out


def test_watch_writes_csv_and_observes_pace(monkeypatch, capsys, tmp_path):
    fake = FakeChain(target=1 << 208, price_wei=10**16, total=12000)
    real = fake.rpc

    def bumping(url, method, params, timeout=None):
        if method == "eth_gasPrice":
            fake.total += 4  # someone else mints between samples
        return real(url, method, params, timeout)

    monkeypatch.setattr(chain, "rpc", bumping)
    csv_path = tmp_path / "trend.csv"
    assert worth_it.main(["--watch", "0.01", "--iterations", "3", "--csv", str(csv_path)]) == 0
    rows = csv_path.read_text().strip().splitlines()
    assert rows[0].startswith("time,minted")
    assert len(rows) == 4
    assert "observed" in capsys.readouterr().out
    assert rows[-1].split(",")[4] != ""  # pace column filled once cats were seen


def test_wrong_chain_refused(monkeypatch):
    fake = FakeChain(chain_id=1)
    monkeypatch.setattr(chain, "rpc", fake.rpc)
    try:
        worth_it.main([])
    except SystemExit as exc:
        assert "Robinhood" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
