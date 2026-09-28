"""Auto-start service files (launchd / systemd / Task Scheduler), install/uninstall with a fake
runner, the deploy/ examples, and the real uninstall script on a throwaway copy."""

import os
import plistlib
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from trader import service
from trader.cli import app
from trader.paths import Paths

REPO = Path(__file__).resolve().parent.parent
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


class FakeRunner:
    def __init__(self, fail: set[str] = frozenset()):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, cmd):
        self.calls.append(cmd)
        rc = 1 if any(f in " ".join(cmd) for f in self.fail) else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")


@pytest.fixture
def odd(tmp_path):
    """A project folder whose path has a space, an '&' and a '%' in it."""
    root = tmp_path / "My Trader & Co 100%"
    root.mkdir()
    runner = FakeRunner()
    ctx = service.make_ctx(Paths(root), home=tmp_path / "home", run=runner, python=str(root / ".venv/bin/python"))
    return SimpleNamespace(root=root, ctx=ctx, runner=runner, home=tmp_path / "home")


# ------------------------------------------------------------------------------ generators
def test_launchd_plists_restart_on_crash_only_and_survive_odd_paths(odd):
    files = service.launchd_plists(odd.ctx)
    trader = plistlib.loads(files["com.cryptotrade.trader.plist"])
    assert trader["ProgramArguments"] == [str(odd.root / ".venv/bin/python"), "-m", "trader", "serve"]
    assert trader["RunAtLoad"] is True
    assert trader["KeepAlive"] == {"SuccessfulExit": False}  # restart after a crash, not after `stop`
    assert trader["EnvironmentVariables"] == {"TRADER_HOME": str(odd.root)}
    assert trader["WorkingDirectory"] == str(odd.root)
    hb = plistlib.loads(files["com.cryptotrade.trader.heartbeat.plist"])
    assert hb["ProgramArguments"][-1] == "check-heartbeat" and hb["StartInterval"] == 3600
    assert "KeepAlive" not in hb and hb["StandardOutPath"].endswith("heartbeat-check.out")


def test_systemd_units_quote_paths_and_escape_specifiers(odd):
    units = service.systemd_units(odd.ctx)
    svc = units["cryptotrade-trader.service"]
    root = str(odd.root).replace("%", "%%")
    assert f'ExecStart="{root}/.venv/bin/python" -m trader serve' in svc
    assert f'Environment="TRADER_HOME={root}"' in svc and f"WorkingDirectory={root}\n" in svc
    assert "Restart=on-failure" in svc and "WantedBy=default.target" in svc
    assert "0.0.0.0" not in svc
    hb = units["cryptotrade-heartbeat.service"]
    assert "Type=oneshot" in hb and "check-heartbeat" in hb and "SuccessExitStatus=1" in hb
    timer = units["cryptotrade-heartbeat.timer"]
    assert "OnUnitActiveSec=1h" in timer and "Persistent=true" in timer


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_systemd_units_pass_systemd_analyze_verify(tmp_path):
    root = tmp_path / "dir with space & 100%"
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to(sys.executable)
    ctx = service.make_ctx(Paths(root), python=str(root / ".venv/bin/python"))
    out = tmp_path / "units"
    out.mkdir()
    for name, text in service.systemd_units(ctx).items():
        (out / name).write_text(text)
    for name in service.systemd_units(ctx):
        r = subprocess.run(["systemd-analyze", "verify", str(out / name)], capture_output=True, text=True)
        assert r.returncode == 0 and "error" not in r.stderr.lower(), (name, r.stderr)


def test_systemd_refuses_paths_it_cannot_run(tmp_path):
    ctx = service.make_ctx(Paths(tmp_path / 'it\'s "here"'), python="/usr/bin/python3", run=FakeRunner())
    with pytest.raises(ValueError, match="move the CryptoTrade folder"):
        service.systemd_units(ctx)


def test_windows_task_xml_is_valid_and_escaped(odd):
    for hb in (False, True):
        xml = service.windows_task_xml(odd.ctx, heartbeat=hb)
        tree = ET.fromstring(xml.encode("utf-16"))
        ex = tree.find(f"{TASK_NS}Actions/{TASK_NS}Exec")
        assert ex.find(f"{TASK_NS}Command").text == str(odd.root / ".venv/bin/python")  # '&' round-trips
        assert ex.find(f"{TASK_NS}WorkingDirectory").text == str(odd.root)
        args = ex.find(f"{TASK_NS}Arguments").text
        assert args == ("-m trader check-heartbeat" if hb else "-m trader serve")
        trig = tree.find(f"{TASK_NS}Triggers")[0].tag
        assert trig == (f"{TASK_NS}CalendarTrigger" if hb else f"{TASK_NS}LogonTrigger")
        assert tree.find(f".//{TASK_NS}RunLevel").text == "LeastPrivilege"  # never elevated


def test_deploy_examples_match_the_generators():
    sys.path.insert(0, str(REPO / "deploy"))
    try:
        import generate_examples
    finally:
        sys.path.pop(0)
    rendered = generate_examples.render()
    assert len(rendered) == 7
    for rel, data in rendered.items():
        assert (REPO / "deploy" / rel).read_bytes() == data, f"deploy/{rel} is stale: run deploy/generate_examples.py"


# ------------------------------------------------------------------------------ install / uninstall
def test_launchd_install_uninstall(odd):
    uid = str(os.getuid()) if hasattr(os, "getuid") else "501"
    msgs = service.install(odd.ctx, "launchd")
    agents = odd.home / "Library/LaunchAgents"
    assert sorted(p.name for p in agents.iterdir()) == ["com.cryptotrade.trader.heartbeat.plist",
                                                         "com.cryptotrade.trader.plist"]
    assert odd.runner.calls == [
        ["launchctl", "bootout", f"gui/{uid}/com.cryptotrade.trader"],
        ["launchctl", "bootstrap", f"gui/{uid}", str(agents / "com.cryptotrade.trader.plist")],
        ["launchctl", "bootout", f"gui/{uid}/com.cryptotrade.trader.heartbeat"],
        ["launchctl", "bootstrap", f"gui/{uid}", str(agents / "com.cryptotrade.trader.heartbeat.plist")],
    ]
    assert all(m.startswith("✅") for m in msgs[:2])
    assert service.installed(odd.ctx, "launchd")
    odd.runner.calls.clear()
    service.install(odd.ctx, "launchd")  # re-install replaces, never duplicates
    assert len(list(agents.iterdir())) == 2
    odd.runner.calls.clear()
    msgs = service.uninstall(odd.ctx, "launchd")
    assert list(agents.iterdir()) == [] and not service.installed(odd.ctx, "launchd")
    assert [c[:2] for c in odd.runner.calls] == [["launchctl", "bootout"]] * 2
    assert service.uninstall(odd.ctx, "launchd") == ["no auto-start service was installed"]


def test_launchd_failure_is_reported(odd):
    odd.ctx.run = FakeRunner(fail={"bootstrap"})
    msgs = service.install(odd.ctx, "launchd")
    assert msgs[0].startswith("❌") and "boom" in msgs[0]


def test_systemd_install_uninstall(odd):
    service.install(odd.ctx, "systemd")
    d = odd.home / ".config/systemd/user"
    assert sorted(p.name for p in d.iterdir()) == ["cryptotrade-heartbeat.service", "cryptotrade-heartbeat.timer",
                                                    "cryptotrade-trader.service"]
    assert odd.runner.calls == [["systemctl", "--user", "daemon-reload"],
                                ["systemctl", "--user", "enable", "--now", "cryptotrade-trader.service",
                                 "cryptotrade-heartbeat.timer"]]
    assert service.installed(odd.ctx, "systemd")
    odd.runner.calls.clear()
    service.uninstall(odd.ctx, "systemd")
    assert list(d.iterdir()) == [] and not service.installed(odd.ctx, "systemd")
    assert odd.runner.calls == [["systemctl", "--user", "disable", "--now", "cryptotrade-trader.service",
                                 "cryptotrade-heartbeat.timer"], ["systemctl", "--user", "daemon-reload"]]


def test_windows_install_uninstall_commands(odd):
    service.install(odd.ctx, "windows")
    xmls = sorted(odd.root.glob("run/*-task.xml"))
    assert [p.name for p in xmls] == ["heartbeat-task.xml", "trader-task.xml"]
    assert xmls[0].read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 with BOM, as declared
    assert odd.runner.calls == [
        ["schtasks", "/Create", "/F", "/TN", r"CryptoTrade\Trader", "/XML", str(odd.root / "run/trader-task.xml")],
        ["schtasks", "/Create", "/F", "/TN", r"CryptoTrade\Heartbeat", "/XML", str(odd.root / "run/heartbeat-task.xml")],
    ]
    odd.runner.calls.clear()
    service.uninstall(odd.ctx, "windows")
    assert odd.runner.calls == [["schtasks", "/Delete", "/F", "/TN", r"CryptoTrade\Trader"],
                                ["schtasks", "/Delete", "/F", "/TN", r"CryptoTrade\Heartbeat"]]


def test_default_python_is_the_projects_venv(tmp_path):
    ctx = service.make_ctx(Paths(tmp_path))
    assert ctx.python == sys.executable or ctx.python.endswith("pythonw.exe")
    venv_py = tmp_path / ".venv" / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
    venv_py.parent.mkdir(parents=True)
    venv_py.touch()
    assert service.make_ctx(Paths(tmp_path)).python == str(venv_py)


def test_service_cli_status_install_uninstall(home, monkeypatch):
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    fake = FakeRunner()
    real_make = service.make_ctx
    monkeypatch.setattr(service, "make_ctx", lambda paths, **kw: real_make(paths, home=home.root / "userhome", run=fake))
    monkeypatch.setattr(service, "platform_kind", lambda: "systemd")
    r = runner.invoke(app, ["service", "status"])
    assert r.exit_code == 3 and "not installed" in r.output
    r = runner.invoke(app, ["service", "install"])
    assert r.exit_code == 0 and "enable --now" in r.output, r.output
    assert runner.invoke(app, ["service", "status"]).exit_code == 0
    fake.fail = {"enable"}
    r = runner.invoke(app, ["service", "install"])
    assert r.exit_code == 1 and "❌" in r.output
    r = runner.invoke(app, ["service", "uninstall"])
    assert r.exit_code == 0 and "removed systemd user units" in r.output
    assert runner.invoke(app, ["service", "status"]).exit_code == 3


# ------------------------------------------------------------------------------ uninstall script
def _throwaway_install(tmp_path) -> Path:
    """A project folder with the real uninstall script and a .venv/bin/python that runs this code."""
    root = tmp_path / "proj"
    (root / ".venv/bin").mkdir(parents=True)
    shutil.copy(REPO / "uninstall.sh", root / "uninstall.sh")
    for name in ("config.example.yaml", ".env.example"):
        shutil.copy(REPO / name, root / name)
    py = root / ".venv/bin/python"
    py.write_text(f'#!/bin/sh\nPYTHONPATH="{REPO}" exec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    (root / "trader.egg-info").mkdir()
    return root


def _run_uninstall(root, *args, stdin=""):
    env = dict(os.environ, TRADER_HOME=str(root), HOME=str(root / "userhome"))
    env.pop("TRADER_FAKE_NOW", None)
    return subprocess.run([str(root / "uninstall.sh"), *args], env=env, input=stdin, capture_output=True,
                          text=True, timeout=120)


@pytest.mark.skipif(os.name == "nt", reason="POSIX script")
@pytest.mark.parametrize("args,stdin,kept", [(["--keep-data"], "", True), (["--delete-data"], "", False),
                                             ([], "\n", True), ([], "y\n", False)])
def test_uninstall_script(tmp_path, args, stdin, kept):
    root = _throwaway_install(tmp_path)
    env = dict(os.environ, TRADER_HOME=str(root))
    subprocess.run([str(root / ".venv/bin/python"), "-m", "trader", "init"], env=env, check=True, capture_output=True)
    (root / "data/marker.txt").write_text("trade history")
    r = _run_uninstall(root, *args, stdin=stdin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / ".venv").exists() and not (root / "trader.egg-info").exists()
    assert (root / "data/marker.txt").exists() is kept
    assert (root / "config.yaml").exists() and (root / ".env").exists()  # never silently deleted
    assert not (root / "run").exists()  # runtime state incl. the private stop token
    if not args:
        assert "Delete" in r.stdout and "cannot be undone" in r.stdout  # it asked
    assert "removed .venv" in r.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX script")
def test_uninstall_works_with_a_broken_config(tmp_path):
    root = _throwaway_install(tmp_path)
    (root / "data").mkdir()
    (root / "config.yaml").write_text("mode: live\n")  # not even valid for us: must still uninstall
    r = _run_uninstall(root, "--keep-data")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "config.yaml unusable" in r.stderr and not (root / ".venv").exists() and (root / "data").exists()


def test_docker_build_never_bakes_in_secrets_or_state():
    ignored = {line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()}
    assert {".env", "config.yaml", "data/", "run/", "logs/", ".venv/"} <= ignored
    dockerfile = (REPO / "deploy/Dockerfile").read_text()
    build_steps = [ln for ln in dockerfile.splitlines() if ln.startswith("RUN")]
    assert not any("trader init" in ln for ln in build_steps)  # tokens are created at run time, not in the image
    assert "USER trader" in dockerfile and "0.0.0.0" not in dockerfile.split("FROM", 1)[1]
