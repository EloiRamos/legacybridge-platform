"""SEC-14/SEC-15/SEC-16 against the ``default`` profile (``make up`` running): ``make security-compose``.

* SEC-16 — only ``127.0.0.1:8080`` is published; the database and the simulators are not.
* SEC-14 — the simulation control planes are absent: HALDEN ``/_sim/*`` answers 404 and the SKARVIK control port
  is closed, observed from inside the application container (the only container that reaches them).
* SEC-15 — the running default-profile API serves no demo endpoints.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def compose(*args: str, env: dict[str, str] | None = None) -> str:
    import os

    full_env = {k: v for k, v in os.environ.items() if not k.startswith(("LBF_", "SKARVIK_", "HALDEN_"))}
    full_env.update(env or {})
    return subprocess.run(["docker", "compose", *args], cwd=REPO, check=True, capture_output=True, text=True,
                          env=full_env).stdout


def test_sec16_only_loopback_8080_is_published_in_the_default_profile():
    config = json.loads(compose("config", "--format", "json"))
    published = {name: [(p.get("host_ip"), p.get("published"), p.get("target")) for p in svc.get("ports") or []]
                 for name, svc in config["services"].items()}
    assert published.pop("app") == [("127.0.0.1", "8080", 8080)]
    assert all(not ports for ports in published.values()), published
    assert "wiretap" not in config["services"]  # test profile only


def test_sec14_simulation_control_is_off_in_the_default_profile_config():
    config = json.loads(compose("config", "--format", "json"))
    assert config["services"]["halden"]["environment"]["HALDEN_SIM_CONTROL"] == "false"
    assert config["services"]["skarvik"]["environment"]["SKARVIK_SIM_CONTROL"] == "false"
    assert config["services"]["app"]["environment"]["LBF_PROFILE"] == "default"


PROBE = r"""
import socket, urllib.request, urllib.error
try:
    urllib.request.urlopen(urllib.request.Request('http://halden:8090/_sim/faults'), timeout=5)
    halden = 'OPEN'
except urllib.error.HTTPError as e:
    halden = str(e.code)
s = socket.socket(); s.settimeout(3)
skarvik = 'OPEN' if s.connect_ex(('skarvik', 8095)) == 0 else 'CLOSED'
print(halden, skarvik)
"""


def test_sec14_control_planes_unreachable_from_the_running_app():
    out = compose("exec", "-T", "app", "python", "-c", PROBE).split()
    assert out == ["404", "CLOSED"], out


def test_sec15_running_default_api_has_no_demo_endpoints():
    for path in ("/api/v1/demo/reset", "/api/v1/demo/faults"):
        req = urllib.request.Request(f"http://127.0.0.1:8080{path}", method="POST",
                                     headers={"Origin": "http://127.0.0.1:8080"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)  # noqa: S310 - loopback
        assert exc.value.code == 404, (path, exc.value.code)  # absent, not merely protected
    spec = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/api/v1/openapi.json", timeout=5).read())
    assert not [p for p in spec["paths"] if "/demo/" in p]
