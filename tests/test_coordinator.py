import json
import shutil
import subprocess
from pathlib import Path

import pytest
from eth_account import Account

import chain
import coordinator
import pow as hpow
from fake_chain import FakeChain

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not available")


@pytest.fixture(scope="module")
def cpu_worker(tmp_path_factory):
    binary = tmp_path_factory.mktemp("build") / "cpu_worker"
    subprocess.run(["g++", "-O2", "-std=c++17", "-o", str(binary), str(ROOT / "miner" / "cpu_worker.cpp")], check=True)
    return binary


def make(tmp_path, cpu_worker, fake, monkeypatch, **overrides):
    monkeypatch.setattr(chain, "rpc", fake.rpc)
    account = Account.create()
    key_file = tmp_path / "wallet.key"
    key_file.write_text(account.key.hex() + "\n")
    fake.fund(account.address, 10**18)
    params = dict(live=True, local_cpu=True, worker=str(cpu_worker), gpus=2, batch_log2=10,
                  target_mints=2, max_price_eth=0.01, max_spend_eth=0.05, max_hours=0.02,
                  state_dir=tmp_path / "state", poll_ms=20, key_file=str(key_file),
                  miner_address=account.address, stats_seconds=0.3)
    params.update(overrides)
    cfg = coordinator.Config(**params)
    return cfg, coordinator.load_account(cfg), account


def test_live_mints_until_target(tmp_path, cpu_worker, monkeypatch):
    fake = FakeChain(target=1 << 248, price_wei=10**16, total=12000)
    cfg, account, _ = make(tmp_path, cpu_worker, fake, monkeypatch)
    result = coordinator.Coordinator(cfg, account).run()
    assert result["reason"] == "target mints reached"
    assert result["minted"] == 2 and result["reverts"] == 0
    assert fake.total == 12002
    assert [tx["value"] for tx in fake.txs] == [10**16, 10**16]
    receipts = json.loads((cfg.state_dir / "receipts.json").read_text())
    assert [r["token_id"] for r in receipts] == [12000, 12001]
    assert fake.owners[12000].lower() == account.address.lower()
    assert 0.02 < result["spend_eth"] < 0.021
    assert not (cfg.state_dir / "pending.json").exists()
    # the second cat was mined against the halved (streak) target
    assert fake.target_for(account.address) == (1 << 248) // 4
    log = (cfg.state_dir / "log.jsonl").read_text()
    assert "MINTED" in log and "job" in log


def test_dry_run_signs_nothing(tmp_path, cpu_worker, monkeypatch):
    fake = FakeChain(target=1 << 248)
    cfg, _, account = make(tmp_path, cpu_worker, fake, monkeypatch, live=False, target_mints=1)
    result = coordinator.Coordinator(cfg, None).run()
    assert result["reason"] == "target mints reached"
    assert result["live"] is False and fake.txs == []
    log = [json.loads(l) for l in (cfg.state_dir / "log.jsonl").read_text().splitlines()]
    would = [l for l in log if l["type"] == "would_submit"]
    assert len(would) == 1
    assert hpow.verify(account.address, int(would[0]["nonce"], 16), fake.prev, fake.anchor, fake.target_for(account.address))


def test_price_cap_stops_before_spending(tmp_path, cpu_worker, monkeypatch):
    fake = FakeChain(target=1 << 248, price_wei=2 * 10**16)
    cfg, account, _ = make(tmp_path, cpu_worker, fake, monkeypatch)
    result = coordinator.Coordinator(cfg, account).run()
    assert result["reason"].startswith("mint price 0.0200 ETH above")
    assert fake.txs == [] and result["minted"] == 0


def test_stale_candidate_is_skipped(tmp_path, cpu_worker, monkeypatch):
    fake = FakeChain(target=1 << 248)
    cfg, account, _ = make(tmp_path, cpu_worker, fake, monkeypatch)
    co = coordinator.Coordinator(cfg, account)
    co.send = lambda msg: None
    co.refresh_job(chain.Chain().snapshot(account.address))
    nonce, _ = hpow.cpu_search(account.address, fake.prev, fake.anchor, fake.target_for(account.address), 1 << 64, 5000)
    fake.prev += 1  # someone else minted first
    co.handle_candidate({"job": co.job_id, "worker": 0, "nonce": f"0x{nonce:064x}", "digest": "0x00"})
    assert fake.txs == [] and co.stop_reason is None
    co.handle_candidate({"job": co.job_id - 1, "worker": 0, "nonce": f"0x{nonce:064x}", "digest": "0x00"})
    assert fake.txs == []


def test_invalid_candidate_halts(tmp_path, cpu_worker, monkeypatch):
    fake = FakeChain(target=1 << 248)
    cfg, account, _ = make(tmp_path, cpu_worker, fake, monkeypatch)
    co = coordinator.Coordinator(cfg, account)
    co.send = lambda msg: None
    co.refresh_job(chain.Chain().snapshot(account.address))
    co.handle_candidate({"job": co.job_id, "worker": 0, "nonce": "0x" + "ff" * 32, "digest": "0x00"})
    assert co.stop_reason.startswith("worker produced an invalid candidate")
    assert fake.txs == []


def test_revert_counts_and_resumes(tmp_path, cpu_worker, monkeypatch):
    fake = FakeChain(target=1 << 248)
    cfg, account, _ = make(tmp_path, cpu_worker, fake, monkeypatch, max_reverts=2)
    co = coordinator.Coordinator(cfg, account)
    sent = []
    co.send = lambda msg: sent.append(msg)
    co.refresh_job(chain.Chain().snapshot(account.address))
    nonce, _ = hpow.cpu_search(account.address, fake.prev, fake.anchor, fake.target_for(account.address), 1 << 64, 5000)
    fake.anchor_block += 1  # the anchor window moved: the contract will reject our anchorBlock
    co.handle_candidate({"job": co.job_id, "worker": 0, "nonce": f"0x{nonce:064x}", "digest": "0x00"})
    assert co.reverts == 1 and co.stop_reason is None and len(fake.txs) == 1
    assert sent[-1] == {"type": "resume"}
    assert co.spend_wei > 0  # gas was burned
    co.handle_candidate({"job": co.job_id, "worker": 0, "nonce": f"0x{nonce:064x}", "digest": "0x00"})
    assert co.reverts == 2 and co.stop_reason == "too many reverted transactions"


def test_pending_marker_blocks_start(tmp_path, cpu_worker, monkeypatch):
    fake = FakeChain(target=1 << 248)
    cfg, account, _ = make(tmp_path, cpu_worker, fake, monkeypatch)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "pending.json").write_text("{}")
    with pytest.raises(RuntimeError, match="reconcile"):
        coordinator.Coordinator(cfg, account).run()


def test_agent_command_shapes(tmp_path):
    cfg = coordinator.Config(local_cpu=True, worker="/w", gpus=3, batch_log2=12, miner_address="0x" + "ab" * 20,
                             max_price_eth=0.01)
    cmd = cfg.agent_command()
    assert "--cpu" in cmd and cmd[cmd.index("--gpus") + 1] == "3" and cmd[cmd.index("--batch-log2") + 1] == "12"
    cfg = coordinator.Config(ssh_target="gpu@box", ssh_port=2222, remote_dir="/opt/hc", gpus=4,
                             miner_address="0x" + "ab" * 20, max_price_eth=0.01)
    cmd = cfg.agent_command()
    assert cmd[:3] == ["ssh", "-p", "2222"] and cmd[-2] == "gpu@box"
    assert "cd /opt/hc && python3 -u remote_agent.py --worker ./miner/worker --gpus 4 --batch-log2 28" in cmd[-1]
    with pytest.raises(RuntimeError, match="SSH_TARGET"):
        coordinator.Config(ssh_target=None, miner_address="0x" + "ab" * 20, max_price_eth=0.01).agent_command()
