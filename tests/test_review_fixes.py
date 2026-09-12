"""Regression tests for the review findings: tilde paths, receipt robustness,
target changes, and wallet directory permissions."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from eth_account import Account

import chain
import coordinator
import pow as hpow
import remote as remote_mod
from fake_chain import FakeChain

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import make_wallet  # noqa: E402


def test_shell_path_lets_tilde_expand_but_quotes_the_rest():
    assert remote_mod.shell_path("~/hashcatsminoor") == "~/hashcatsminoor"
    assert remote_mod.shell_path("~/hash cats") == "~/'hash cats'"
    assert remote_mod.shell_path("~") == "~"
    assert remote_mod.shell_path("/opt/hc") == "/opt/hc"
    assert remote_mod.shell_path("/opt/h c") == "'/opt/h c'"


def test_shell_path_expands_in_a_real_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "hash cats").mkdir()
    out = subprocess.run(["bash", "-c", f"cd {remote_mod.shell_path('~/hash cats')} && pwd"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == str(tmp_path / "hash cats")


def test_ssh_remote_expands_key_file_and_upload_uses_shell_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = remote_mod.SshRemote("h", 22, "root", "~/.ssh/id_ed25519", log=lambda *_: None)
    assert r.key_file == str(tmp_path / ".ssh" / "id_ed25519")
    assert "-i" in r.ssh_cmd("true") and str(tmp_path) in " ".join(r.ssh_cmd("true"))


def test_agent_command_keeps_tilde_and_expands_ssh_opts(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = coordinator.Config(ssh_target="root@box", remote_dir="~/hashcatsminoor", ssh_opts="-i ~/k.pem",
                             miner_address="0x" + "ab" * 20, max_price_eth=0.01)
    cmd = cfg.agent_command()
    assert cmd[-1].startswith("cd ~/hashcatsminoor && ")
    assert str(tmp_path / "k.pem") in cmd
    cfg = coordinator.Config(ssh_target="root@box", remote_dir="~/hash cats", miner_address="0x" + "ab" * 20,
                             max_price_eth=0.01)
    assert cfg.agent_command()[-1].startswith("cd ~/'hash cats' && ")


def make_coordinator(tmp_path, monkeypatch, fake, **overrides):
    monkeypatch.setattr(chain, "rpc", fake.rpc)
    account = Account.create()
    key = tmp_path / "wallet.key"
    key.write_text(account.key.hex() + "\n")
    fake.fund(account.address, 10**18)
    params = dict(live=True, local_cpu=True, target_mints=2, max_price_eth=0.01, max_spend_eth=0.05,
                  state_dir=tmp_path / "state", key_file=str(key), miner_address=account.address)
    params.update(overrides)
    cfg = coordinator.Config(**params)
    co = coordinator.Coordinator(cfg, coordinator.load_account(cfg))
    co.send = lambda msg: None
    return co, account


def test_target_change_alone_refreshes_the_job(tmp_path, monkeypatch):
    fake = FakeChain(target=1 << 248)
    co, account = make_coordinator(tmp_path, monkeypatch, fake)
    co.refresh_job(chain.Chain().snapshot(account.address))
    first = co.job_id
    co.refresh_job(chain.Chain().snapshot(account.address))
    assert co.job_id == first  # nothing moved
    fake.targets[account.address.lower()] = 1 << 247  # streak halves the target, prevWork unchanged
    co.refresh_job(chain.Chain().snapshot(account.address))
    assert co.job_id == first + 1


def test_candidate_is_rechecked_against_the_live_target(tmp_path, monkeypatch):
    fake = FakeChain(target=1 << 248)
    co, account = make_coordinator(tmp_path, monkeypatch, fake)
    co.refresh_job(chain.Chain().snapshot(account.address))
    # a nonce that passes the job target but not a target 2^40 times harder
    nonce, _ = hpow.cpu_search(account.address, fake.prev, fake.anchor, 1 << 248, 1 << 64, 20000)
    fake.targets[account.address.lower()] = 1 << 208
    co.handle_candidate({"job": co.job_id, "worker": 0, "nonce": f"0x{nonce:064x}", "digest": "0x00"})
    assert fake.txs == [] and co.stop_reason is None
    log = (tmp_path / "state" / "log.jsonl").read_text()
    assert "target moved" in log


def test_receipt_without_effective_gas_price_still_records_the_mint(tmp_path, monkeypatch):
    fake = FakeChain(target=1 << 248)
    co, account = make_coordinator(tmp_path, monkeypatch, fake)
    real = fake.rpc

    def stripped(url, method, params, timeout=None):
        out = real(url, method, params, timeout)
        if method == "eth_getTransactionReceipt" and out:
            out = dict(out)
            out.pop("effectiveGasPrice")
        return out

    monkeypatch.setattr(chain, "rpc", stripped)
    co.refresh_job(chain.Chain().snapshot(account.address))
    nonce, _ = hpow.cpu_search(account.address, fake.prev, fake.anchor, 1 << 248, 1 << 64, 20000)
    co.handle_candidate({"job": co.job_id, "worker": 0, "nonce": f"0x{nonce:064x}", "digest": "0x00"})
    assert len(co.minted) == 1 and co.minted[0]["gas_wei"] > 0
    assert not (tmp_path / "state" / "pending.json").exists()


def test_receipt_without_status_keeps_pending_marker(tmp_path, monkeypatch):
    fake = FakeChain(target=1 << 248)
    co, account = make_coordinator(tmp_path, monkeypatch, fake)
    real = fake.rpc

    def broken(url, method, params, timeout=None):
        out = real(url, method, params, timeout)
        if method == "eth_getTransactionReceipt" and out:
            out = {k: v for k, v in out.items() if k != "status"}
        return out

    monkeypatch.setattr(chain, "rpc", broken)
    co.refresh_job(chain.Chain().snapshot(account.address))
    nonce, _ = hpow.cpu_search(account.address, fake.prev, fake.anchor, 1 << 248, 1 << 64, 20000)
    co.handle_candidate({"job": co.job_id, "worker": 0, "nonce": f"0x{nonce:064x}", "digest": "0x00"})
    assert co.stop_reason.startswith("receipt has no status")
    assert (tmp_path / "state" / "pending.json").exists()


def test_make_wallet_only_locks_down_a_directory_it_created(tmp_path, capsys):
    existing = tmp_path / "home"
    existing.mkdir()
    os.chmod(existing, 0o755)
    assert make_wallet.main(["--out", str(existing / "wallet.key")]) == 0
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert stat.S_IMODE((existing / "wallet.key").stat().st_mode) == 0o600
    assert make_wallet.main(["--out", str(tmp_path / "fresh" / "secrets" / "wallet.key")]) == 0
    assert stat.S_IMODE((tmp_path / "fresh" / "secrets").stat().st_mode) == 0o700
    out = capsys.readouterr().out
    assert out.count("address: 0x") == 2
    # a second run on an existing file does not overwrite it
    before = (existing / "wallet.key").read_text()
    assert make_wallet.main(["--out", str(existing / "wallet.key")]) == 0
    assert (existing / "wallet.key").read_text() == before
