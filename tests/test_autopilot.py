import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from eth_account import Account

import autopilot
import chain
import remote as remote_mod
import vast as vast_mod
from fake_chain import FakeChain

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not available")


@pytest.fixture
def fake_vast(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "vastai"
    exe.write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "fake_vastai.py").read_text())
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "vast_state.json"
    monkeypatch.setenv("FAKE_VAST_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return state


def vast_log(state):
    return json.load(open(state))["log"] if state.exists() else []


def test_vast_wrapper_against_fake_cli(fake_vast):
    cli = vast_mod.VastCLI(log=lambda *_: None)
    assert cli.available()
    offers = cli.search("RTX_4090", 1, 0.5)
    assert [o["id"] for o in offers] == [222, 111]  # sorted by price
    assert offers[0]["dph"] == 0.30 and offers[0]["cuda"] == 12.6
    assert cli.create(222) == 4242
    info = cli.wait_running(4242, timeout=5, poll=0)
    assert info["actual_status"] == "running"
    assert cli.ssh_endpoint(4242) == ("root", "203.0.113.5", 40123)
    cli.destroy(4242)
    log = vast_log(fake_vast)
    assert log[0][:2] == ["search", "offers"] and "dph<=0.5" in log[0][2]
    assert log[1][:3] == ["create", "instance", "222"] and "--ssh" in log[1] and "--direct" in log[1]
    assert log[-1] == ["destroy", "instance", "4242", "-y"]
    assert json.load(open(fake_vast))["destroyed"]


def test_parse_reply_handles_json_and_python_literals():
    assert vast_mod.parse_reply('{"a": 1}') == {"a": 1}
    assert vast_mod.parse_reply("{'success': True, 'new_contract': 7}") == {"success": True, "new_contract": 7}
    assert vast_mod.parse_reply("") is None
    with pytest.raises(vast_mod.VastError):
        vast_mod.parse_reply("not a reply at all <>")


def setup_env(tmp_path, monkeypatch, fake, **extra):
    monkeypatch.setattr(chain, "rpc", fake.rpc)
    account = Account.create()
    key = tmp_path / "wallet.key"
    key.write_text(account.key.hex() + "\n")
    fake.fund(account.address, 10**18)
    values = {"WALLET_KEY_FILE": str(key), "MAX_PRICE_ETH": "0.01", "TARGET_MINTS": "1", "MAX_SPEND_ETH": "0.05",
              "MAX_HOURS": "0.02", "ETH_USD": "2500", "MIN_PROFIT_PER_HOUR": "-1000000000", "MIN_REMAINING": "0",
              "STATE_DIR": str(tmp_path / "state"), "REMOTE_DIR": str(tmp_path / "box")}
    values.update(extra)
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return account


def test_local_cpu_end_to_end_mints(tmp_path, monkeypatch):
    fake = FakeChain(target=1 << 248, price_wei=10**16, total=12000)
    account = setup_env(tmp_path, monkeypatch, fake)
    rc = autopilot.main(["--provider", "local", "--cpu", "--yes"])
    assert rc == 0
    assert fake.total == 12001 and fake.owners[12000].lower() == account.address.lower()
    summaries = list((tmp_path / "state").glob("autopilot-*.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["reason"] == "target mints reached" and summary["minted"] == 1
    assert summary["ghs"] is not None and summary["ghs"] > 0
    assert summary["instance_id"] is None


def test_dry_run_rents_nothing(tmp_path, monkeypatch, fake_vast):
    fake = FakeChain(target=1 << 248)
    setup_env(tmp_path, monkeypatch, fake, SSH_KEY_FILE=str(tmp_path / "wallet.key"))
    rc = autopilot.main(["--provider", "vast", "--dry-run"])
    assert rc == 0
    log = vast_log(fake_vast)
    assert [l[:2] for l in log] == [["show", "user"], ["search", "offers"]]


def test_gate_blocks_on_hard_target(tmp_path, monkeypatch, fake_vast):
    fake = FakeChain(target=1 << 190)
    setup_env(tmp_path, monkeypatch, fake, MIN_PROFIT_PER_HOUR="5", SSH_KEY_FILE=str(tmp_path / "wallet.key"))
    rc = autopilot.main(["--provider", "vast", "--yes"])
    assert rc == 1
    summary = json.loads(next((tmp_path / "state").glob("autopilot-*.json")).read_text())
    assert summary["reason"].startswith("gate failed")
    assert all(l[:2] != ["create", "instance"] for l in vast_log(fake_vast))


def test_vast_rehearsal_rents_sets_up_and_destroys(tmp_path, monkeypatch, fake_vast):
    fake = FakeChain(target=1 << 248)
    setup_env(tmp_path, monkeypatch, fake, SSH_KEY_FILE=str(tmp_path / "wallet.key"))
    box = tmp_path / "box"
    box.mkdir()
    seen = {}

    def fake_ssh_remote(host, port, user="root", key_file=None, extra_opts="", log=print):
        seen.update(host=host, port=port, user=user, key_file=key_file)
        return remote_mod.LocalRemote(box, log=log)

    monkeypatch.setattr(autopilot.remote_mod, "SshRemote", fake_ssh_remote)
    rc = autopilot.main(["--provider", "vast", "--rehearse", "--cpu", "--yes"])
    assert rc == 0
    assert seen == {"host": "203.0.113.5", "port": 40123, "user": "root", "key_file": str(tmp_path / "wallet.key")}
    log = vast_log(fake_vast)
    kinds = [" ".join(l[:2]) for l in log]
    assert kinds[:3] == ["show user", "search offers", "create instance"]
    assert "show instance 4242" in [" ".join(l[:3]) for l in log] and "ssh-url 4242" in kinds
    assert kinds[-1] == "destroy instance"
    assert (box / "miner" / "cpu_worker").exists()  # built on the "box"
    assert fake.txs == []  # rehearsal never signs
    summary = json.loads(next((tmp_path / "state").glob("autopilot-*.json")).read_text())
    assert summary["reason"] == "target mints reached" and summary["instance_id"] == 4242
    assert summary["rental_hours"] > 0 and summary["rental_usd"] > 0


def test_ssh_remote_command_shapes():
    r = remote_mod.SshRemote("203.0.113.5", 40123, "root", "/k/id_ed25519", log=lambda *_: None)
    cmd = r.ssh_cmd("nvidia-smi -L")
    assert cmd[:3] == ["ssh", "-p", "40123"] and "-i" in cmd and cmd[-2:] == ["root@203.0.113.5", "nvidia-smi -L"]
    kw = r.coordinator_kwargs(cpu=False)
    assert kw["ssh_target"] == "root@203.0.113.5" and kw["ssh_port"] == 40123 and "-i /k/id_ed25519" in kw["ssh_opts"]


def test_local_remote_upload_copies_only_worker_files(tmp_path):
    r = remote_mod.LocalRemote(tmp_path / "box", log=lambda *_: None)
    r.upload_repo(ROOT, str(tmp_path / "box"))
    names = sorted(p.relative_to(tmp_path / "box").as_posix() for p in (tmp_path / "box").rglob("*") if p.is_file())
    assert names == sorted(remote_mod.REMOTE_FILES)
    assert not any("coordinator" in n or "secrets" in n for n in names)
