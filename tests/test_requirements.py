"""requirements.txt must be valid PEP 508, exactly pinned, and resolve to exactly one version
of each package on every supported platform (an invalid marker breaks every install)."""

from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent

PLATFORMS = {
    "mac-arm64": {"sys_platform": "darwin", "platform_system": "Darwin", "platform_machine": "arm64"},
    "mac-intel": {"sys_platform": "darwin", "platform_system": "Darwin", "platform_machine": "x86_64"},
    "linux": {"sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64"},
    "windows": {"sys_platform": "win32", "platform_system": "Windows", "platform_machine": "AMD64"},
}


def _reqs(name):
    lines = [l.split(" #")[0].strip() for l in (ROOT / name).read_text().splitlines()]
    return [Requirement(l) for l in lines if l and not l.startswith("#")]


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt", "requirements-llm.txt"])
def test_every_line_is_valid_and_exactly_pinned(name):
    for r in _reqs(name):
        specs = list(r.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", f"{r} is not pinned with =="


@pytest.mark.parametrize("platform", list(PLATFORMS))
@pytest.mark.parametrize("py", ["3.11", "3.12", "3.13", "3.14"])
def test_one_version_per_package_per_platform(platform, py):
    env = dict(PLATFORMS[platform], python_version=py, implementation_name="cpython")
    seen = {}
    for r in _reqs("requirements.txt"):
        if r.marker is None or r.marker.evaluate(env):
            key = r.name.lower().replace("_", "-")
            assert key not in seen, f"{key} pinned twice on {platform}/py{py}"
            seen[key] = str(r.specifier)
    assert "ccxt" in seen and "cryptography" in seen
    expected = "==48.0.1" if platform == "mac-intel" else "==50.0.1"
    assert seen["cryptography"] == expected


def test_optional_ai_packages_never_conflict_with_the_main_pins():
    main = {r.name.lower().replace("_", "-"): str(r.specifier) for r in _reqs("requirements.txt")}
    for r in _reqs("requirements-llm.txt"):
        key = r.name.lower().replace("_", "-")
        assert key not in main, f"{key} is pinned in both files"
    assert any(r.name == "anthropic" for r in _reqs("requirements-llm.txt"))
