# deploy/

Reference copies of the auto-start files. **You normally never touch these.** Run
`.venv/bin/trader service install` and it writes the right files for your computer, with
your real paths, and loads them. These copies show what it generates (with example paths).

| Folder | Platform | Installed to | Status |
|---|---|---|---|
| `launchd/` | macOS | `~/Library/LaunchAgents/` | generated and validated as plists; **not run on a real Mac** |
| `systemd/` | Linux | `~/.config/systemd/user/` | `systemd-analyze verify` passes (including paths with spaces, `&` and `%`); not run under a live systemd |
| `windows/` | Windows | Task Scheduler (`schtasks /XML`, UTF-16) | valid XML; **never run on Windows** |
| `Dockerfile` | Linux + Docker | none | **untested** (see the comments inside) |

What they do:
* start the trader at login;
* restart it only after a crash (a normal `trader stop` keeps it stopped);
* run `trader check-heartbeat` every hour, which sends an urgent email (at most once a day) if
  there has been no heartbeat for more than 26 hours.

The Docker build context excludes `.env`, `config.yaml` and `data/` (see `../.dockerignore`),
so secrets never end up in an image.

Regenerate after changing `trader/service.py`: `.venv/bin/python deploy/generate_examples.py`
(a test fails if these copies drift).
