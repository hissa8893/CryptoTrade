"""`trader service install|uninstall|status`: start the trader automatically at login/boot.

  macOS    launchd agents in ~/Library/LaunchAgents (the trader + an hourly heartbeat check)
  Linux    systemd --user units in ~/.config/systemd/user (service + hourly heartbeat timer)
  Windows  Task Scheduler tasks (at logon + hourly heartbeat check)   [UNTESTED]

Restart policy: restart after a CRASH (non-zero exit) but NOT after a normal `trader stop`
(exit 0), so the stop command keeps working. `serve` exits 0 when another copy is already
running, so starting by hand and at login never fight each other.
Files are generated with proper serialisers (plistlib for launchd), so paths containing
spaces or '&' cannot break them.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from trader.paths import FROZEN, Paths, self_command

LABEL = "com.cryptotrade.trader"
HB_LABEL = "com.cryptotrade.trader.heartbeat"
UNIT = "cryptotrade-trader.service"
HB_UNIT = "cryptotrade-heartbeat.service"
HB_TIMER = "cryptotrade-heartbeat.timer"
WIN_TASK = r"CryptoTrade\Trader"
WIN_HB_TASK = r"CryptoTrade\Heartbeat"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def platform_kind() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if os.name == "nt":
        return "windows"
    return "systemd"


@dataclass
class Ctx:
    paths: Paths
    python: str  # the interpreter (or, in a PyInstaller build, the `trader` executable itself)
    home: Path
    run: Runner

    @property
    def root(self) -> str:
        return str(self.paths.root)

    def argv(self, command: str) -> list[str]:
        return [*self_command(self.python), command]


def _default_python(paths: Paths) -> str:
    if FROZEN:
        return sys.executable
    # the project's own .venv (stable name, survives a Python upgrade), else whatever runs us now
    venv = paths.root / ".venv" / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
    if venv.exists():
        return str(venv)
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("python.exe"):
        py = py[:-10] + "pythonw.exe"  # no console window
    return py


def make_ctx(paths: Paths, *, home: Path | None = None, run: Runner | None = None, python: str | None = None) -> Ctx:
    return Ctx(paths, python or _default_python(paths), home or Path.home(), run or _run)


# ------------------------------------------------------------------------------ file generators
def launchd_plists(ctx: Ctx) -> dict[str, bytes]:
    common = {"WorkingDirectory": ctx.root, "EnvironmentVariables": {"TRADER_HOME": ctx.root},
              "StandardOutPath": str(ctx.paths.logs / "serve.out"), "StandardErrorPath": str(ctx.paths.logs / "serve.out")}
    trader = {"Label": LABEL, "ProgramArguments": ctx.argv("serve"), "RunAtLoad": True,
              "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "ProcessType": "Background", **common}
    hb = {"Label": HB_LABEL, "ProgramArguments": ctx.argv("check-heartbeat"), "StartInterval": 3600,
          "RunAtLoad": False, "ProcessType": "Background", **common,
          "StandardOutPath": str(ctx.paths.logs / "heartbeat-check.out"),
          "StandardErrorPath": str(ctx.paths.logs / "heartbeat-check.out")}
    return {f"{LABEL}.plist": plistlib.dumps(trader), f"{HB_LABEL}.plist": plistlib.dumps(hb)}


SYSTEMD_BAD_CHARS = "\"'\\\n"  # systemd refuses these in an executable path, however they are quoted


def _sd_path(s: str) -> str:
    return s.replace("%", "%%")  # '%' starts a systemd specifier


def _sd_quote(s: str) -> str:
    return f'"{_sd_path(s)}"'  # quotes keep spaces; '$' in the executable is taken literally (checked)


def systemd_units(ctx: Ctx) -> dict[str, str]:
    bad = sorted({c for c in ctx.root + ctx.python if c in SYSTEMD_BAD_CHARS})
    if bad:
        raise ValueError(f"systemd cannot run a program from a folder whose path contains {' '.join(map(repr, bad))}; "
                         f"move the CryptoTrade folder to a simpler path and run the installer again")
    env = f"Environment={_sd_quote('TRADER_HOME=' + ctx.root)}"

    def exec_start(command: str) -> str:
        exe, *args = ctx.argv(command)
        return "ExecStart=" + " ".join([_sd_quote(exe), *args])
    return {
        UNIT: "\n".join([
            "[Unit]", "Description=CryptoTrade paper trader (SIMULATION ONLY - never places real orders)",
            "After=network-online.target", "Wants=network-online.target", "",
            "[Service]", "Type=simple", f"WorkingDirectory={_sd_path(ctx.root)}", env,
            exec_start("serve"),
            "# restart after a crash, but not after a normal `trader stop` (exit code 0)",
            "Restart=on-failure", "RestartSec=30", "TimeoutStopSec=45", "", "[Install]", "WantedBy=default.target", ""]),
        HB_UNIT: "\n".join([
            "[Unit]", "Description=CryptoTrade paper trader heartbeat check (alerts if no heartbeat for 26 h)", "",
            "[Service]", "Type=oneshot", f"WorkingDirectory={_sd_path(ctx.root)}", env,
            exec_start("check-heartbeat"), "SuccessExitStatus=1", ""]),
        HB_TIMER: "\n".join([
            "[Unit]", "Description=Hourly CryptoTrade heartbeat check", "",
            "[Timer]", "OnBootSec=15min", "OnUnitActiveSec=1h", "Persistent=true", "",
            "[Install]", "WantedBy=timers.target", ""]),
    }


def windows_task_xml(ctx: Ctx, *, heartbeat: bool = False) -> str:
    trigger = ("<CalendarTrigger><StartBoundary>2026-01-01T00:05:00</StartBoundary><Repetition><Interval>PT1H</Interval>"
               "</Repetition><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>"
               if heartbeat else "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>")
    exe, *args = ctx.argv("check-heartbeat" if heartbeat else "serve")
    restart = "" if heartbeat else "<RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>CryptoTrade paper trader {'heartbeat check' if heartbeat else '(simulation only)'}</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>{restart}</Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{' '.join(args)}</Arguments>
    <WorkingDirectory>{escape(ctx.root)}</WorkingDirectory></Exec></Actions>
</Task>
"""


# ------------------------------------------------------------------------------ install / uninstall
def _launchd_dir(ctx: Ctx) -> Path:
    return ctx.home / "Library" / "LaunchAgents"


def _systemd_dir(ctx: Ctx) -> Path:
    return ctx.home / ".config" / "systemd" / "user"


def install(ctx: Ctx, kind: str | None = None) -> list[str]:
    kind = kind or platform_kind()
    ctx.paths.logs.mkdir(parents=True, exist_ok=True)
    msgs = []
    if kind == "launchd":
        d = _launchd_dir(ctx)
        d.mkdir(parents=True, exist_ok=True)
        uid = str(os.getuid()) if hasattr(os, "getuid") else "501"
        for name, data in launchd_plists(ctx).items():
            f = d / name
            label = name[: -len(".plist")]
            ctx.run(["launchctl", "bootout", f"gui/{uid}/{label}"])  # replace an older copy (ok if absent)
            f.write_bytes(data)
            r = ctx.run(["launchctl", "bootstrap", f"gui/{uid}", str(f)])
            msgs.append(f"{'✅' if r.returncode == 0 else '❌'} launchd agent {label} -> {f}"
                        + ("" if r.returncode == 0 else f" ({(r.stderr or r.stdout).strip()})"))
        msgs.append("The trader now starts at every login and restarts after a crash; `stop` still stops it.")
    elif kind == "systemd":
        d = _systemd_dir(ctx)
        d.mkdir(parents=True, exist_ok=True)
        for name, text in systemd_units(ctx).items():
            (d / name).write_text(text)
        steps = [["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "--now", UNIT, HB_TIMER]]
        for cmd in steps:
            r = ctx.run(cmd)
            msgs.append(f"{'✅' if r.returncode == 0 else '❌'} {' '.join(cmd)}"
                        + ("" if r.returncode == 0 else f" ({(r.stderr or r.stdout).strip()})"))
        msgs.append(f"Units written to {d}. To also start at boot without logging in: loginctl enable-linger $USER")
    elif kind == "windows":
        for task, hb in ((WIN_TASK, False), (WIN_HB_TASK, True)):
            xml = ctx.paths.run / (task.split("\\")[-1].lower() + "-task.xml")
            ctx.paths.run.mkdir(parents=True, exist_ok=True)
            xml.write_text(windows_task_xml(ctx, heartbeat=hb), encoding="utf-16")
            r = ctx.run(["schtasks", "/Create", "/F", "/TN", task, "/XML", str(xml)])
            msgs.append(f"{'✅' if r.returncode == 0 else '❌'} scheduled task {task} (UNTESTED on Windows)")
    else:
        raise ValueError(f"unknown service kind {kind}")
    return msgs


def uninstall(ctx: Ctx, kind: str | None = None) -> list[str]:
    kind = kind or platform_kind()
    msgs = []
    if kind == "launchd":
        uid = str(os.getuid()) if hasattr(os, "getuid") else "501"
        for label in (LABEL, HB_LABEL):
            f = _launchd_dir(ctx) / f"{label}.plist"
            if f.exists():
                ctx.run(["launchctl", "bootout", f"gui/{uid}/{label}"])
                f.unlink()
                msgs.append(f"removed launchd agent {label}")
    elif kind == "systemd":
        d = _systemd_dir(ctx)
        if any((d / n).exists() for n in (UNIT, HB_UNIT, HB_TIMER)):
            ctx.run(["systemctl", "--user", "disable", "--now", UNIT, HB_TIMER])
            for n in (UNIT, HB_UNIT, HB_TIMER):
                (d / n).unlink(missing_ok=True)
            ctx.run(["systemctl", "--user", "daemon-reload"])
            msgs.append(f"removed systemd user units from {d}")
    elif kind == "windows":
        for task in (WIN_TASK, WIN_HB_TASK):
            r = ctx.run(["schtasks", "/Delete", "/F", "/TN", task])
            if r.returncode == 0:
                msgs.append(f"removed scheduled task {task}")
    return msgs or ["no auto-start service was installed"]


def installed(ctx: Ctx, kind: str | None = None) -> bool:
    kind = kind or platform_kind()
    if kind == "launchd":
        return (_launchd_dir(ctx) / f"{LABEL}.plist").exists()
    if kind == "systemd":
        return (_systemd_dir(ctx) / UNIT).exists()
    return ctx.run(["schtasks", "/Query", "/TN", WIN_TASK]).returncode == 0
