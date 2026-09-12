import sys
from pathlib import Path

import pytest
from eth_account import Account

import chain
from fake_chain import FakeChain

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sweep  # noqa: E402


def setup(tmp_path, monkeypatch, balance=10**17, gas_price=10**8):
    fake = FakeChain(gas_price=gas_price)
    monkeypatch.setattr(chain, "rpc", fake.rpc)
    account = Account.create()
    fake.fund(account.address, balance)
    key = tmp_path / "wallet.key"
    key.write_text(account.key.hex() + "\n")
    return fake, account, str(key)


def test_sweep_sends_everything_minus_gas(tmp_path, monkeypatch, capsys):
    fake, account, key = setup(tmp_path, monkeypatch, balance=10**17)
    dest = Account.create().address
    rc = sweep.main([dest, "--key-file", key, "--yes"])
    assert rc == 0
    tx = fake.txs[-1]
    fee = 21000 * fake.gas_price
    assert tx["to"].hex().lower() == dest[2:].lower()
    assert tx["value"] == 10**17 - fee
    assert tx["nonce"] == 0
    assert fake.balances[dest.lower()] == 10**17 - fee
    assert fake.balances[account.address.lower()] == 0
    assert "confirmed" in capsys.readouterr().out


def test_sweep_fixed_amount_leaves_remainder(tmp_path, monkeypatch):
    fake, account, key = setup(tmp_path, monkeypatch, balance=10**17)
    dest = Account.create().address
    assert sweep.main([dest, "--key-file", key, "--amount", "0.02", "--yes"]) == 0
    assert fake.balances[dest.lower()] == 2 * 10**16
    remaining = fake.balances[account.address.lower()]
    assert 0 < remaining < 8 * 10**16  # the 0.08 remainder minus gas


def test_sweep_refuses_empty_wallet(tmp_path, monkeypatch):
    fake, account, key = setup(tmp_path, monkeypatch, balance=1000)
    with pytest.raises(SystemExit, match="nothing to send"):
        sweep.main([Account.create().address, "--key-file", key, "--yes"])


def test_sweep_rejects_bad_address(tmp_path, monkeypatch):
    fake, account, key = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="not a valid address"):
        sweep.main(["not-an-address", "--key-file", key, "--yes"])


def test_sweep_rejects_wrong_chain(tmp_path, monkeypatch):
    fake, account, key = setup(tmp_path, monkeypatch)
    fake.chain_id = 1
    with pytest.raises(SystemExit, match="not Robinhood Chain"):
        sweep.main([Account.create().address, "--key-file", key, "--yes"])
