"""The deployment configuration contract.

* every active variable in ``.env.example`` is consumed by ``compose.yaml``; ``LBF_COOKIE_SECURE`` reaches the
  application through compose (default ``false``, unchanged) and drives the session cookie's ``Secure`` attribute.
* the HALDEN credential is a Docker secret mounted into both ``app`` and ``halden`` (generated
  ``deploy/secrets/halden_secret.txt``; the test overlay swaps in the public fictional fixture); no environment value
  or default for it remains in the rendered configuration; the simulator's deploy-side entrypoint refuses to start
  without a non-empty secret and never prints it.

Static parts render ``docker compose config`` (no containers are started). The cookie test needs the test database.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO / "deploy" / "halden" / "secret_entrypoint.sh"
DEMO_DEFAULT = "halden-demo-" + "secret"  # the frozen simulator's built-in fallback (HaldenConfig.java)


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LBF_", "SKARVIK_", "HALDEN_", "OPENAI_"))}
    env.update(extra or {})
    return env


def render(*files: str, env: dict[str, str] | None = None) -> dict:
    args = [a for f in files for a in ("-f", f)]
    out = subprocess.run(["docker", "compose", *args, "--profile", "test", "config", "--format", "json"], cwd=REPO,
                         check=True, capture_output=True, text=True, env=_clean_env(env)).stdout
    return json.loads(out)


def env_example_keys() -> list[str]:
    keys = []
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            keys.append(m.group(1))
    return keys


# -----------------------------------------------------------------------------------------------


def test_mi1_every_active_env_example_variable_is_consumed_by_compose():
    compose_text = (REPO / "compose.yaml").read_text(encoding="utf-8")
    keys = env_example_keys()
    assert keys, ".env.example has no active variables"
    unused = [k for k in keys if "${" + k not in compose_text]
    assert unused == [], f".env.example names compose.yaml does not consume: {unused}"


def test_mi1_env_example_values_equal_compose_defaults():
    config = render("compose.yaml")
    app_env = config["services"]["app"]["environment"]
    example = dict(line.split("=", 1) for line in (REPO / ".env.example").read_text("utf-8").splitlines()
                   if re.match(r"^[A-Z][A-Z0-9_]*=", line))
    assert app_env["LBF_COOKIE_SECURE"] == example["LBF_COOKIE_SECURE"] == "false"
    assert app_env["LBF_PUBLIC_ORIGIN"] == example["LBF_PUBLIC_ORIGIN"] == "http://127.0.0.1:8080"
    assert app_env["LBF_HALDEN_URL"] == example["LBF_HALDEN_URL"]
    assert app_env["LBF_TRANSPORT_TIMEOUT_SEC"] == example["LBF_TRANSPORT_TIMEOUT_SEC"]
    assert app_env["LBF_PROFILE"] == example["LBF_PROFILE"] == "default"
    assert app_env["LBF_LIVE_INFERENCE"] == "false"  # still pinned


@pytest.mark.parametrize(("value", "expected"), [(None, "false"), ("true", "true"), ("false", "false")])
def test_mi1_cookie_secure_is_propagated_to_the_app(value, expected):
    config = render("compose.yaml", env={"LBF_COOKIE_SECURE": value} if value else None)
    assert config["services"]["app"]["environment"]["LBF_COOKIE_SECURE"] == expected


def test_mi1_app_config_reads_the_flag(monkeypatch):
    from lbf.api.services import AppConfig

    monkeypatch.setenv("LBF_COOKIE_SECURE", "true")
    assert AppConfig.from_env().cookie_secure is True
    monkeypatch.setenv("LBF_COOKIE_SECURE", "false")
    assert AppConfig.from_env().cookie_secure is False
    monkeypatch.delenv("LBF_COOKIE_SECURE")
    assert AppConfig.from_env().cookie_secure is False  # default unchanged


@pytest.mark.db
@pytest.mark.parametrize("secure", [True, False])
def test_mi1_session_cookie_secure_attribute_follows_the_flag(app_engine, tmp_path, secure):
    from api_support import ORIGIN, Api
    from support import PASSWORD
    from workflow_support import WF

    api = Api(WF(app_engine, tmp_path))
    api.config.cookie_secure = secure
    r = api.anon().post("/api/v1/auth/login", json={"username": "daniel.sales", "password": PASSWORD},
                        headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    cookie = next(v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("lbf_session="))
    attrs = {a.strip().lower() for a in cookie.split(";")}
    assert ("secure" in attrs) is secure
    assert "httponly" in attrs and "samesite=strict" in attrs  # unchanged session hardening


# -----------------------------------------------------------------------------------------------


def test_mi2_default_and_demo_mount_the_generated_secret_into_app_and_halden():
    for env in (None, {"LBF_PROFILE": "demo", "LBF_SIM_CONTROL": "true"}):
        config = render("compose.yaml", env=env)
        assert Path(config["secrets"]["halden_secret"]["file"]) == REPO / "deploy" / "secrets" / "halden_secret.txt"
        app, halden = config["services"]["app"], config["services"]["halden"]
        assert "halden_secret" in {s["source"] for s in app["secrets"]}
        assert "halden_secret" in {s["source"] for s in halden["secrets"]}
        assert app["environment"]["LBF_HALDEN_SECRET_FILE"] == "/run/secrets/halden_secret"
        assert "LBF_HALDEN_SECRET" not in app["environment"]  # no environment value
        assert "HALDEN_SECRET" not in (halden.get("environment") or {})
        assert halden["entrypoint"] == ["/bin/sh", "/opt/lbf-deploy/halden_secret_entrypoint.sh"]
        assert DEMO_DEFAULT not in json.dumps(config)


def test_mi2_halden_secret_is_not_overridable_from_the_environment():
    config = render("compose.yaml", env={"HALDEN_SECRET": "x" * 16, "LBF_HALDEN_SECRET": "y" * 16})
    assert "x" * 16 not in json.dumps(config) and "y" * 16 not in json.dumps(config)


def test_mi2_test_overlay_uses_the_public_fixture_and_never_the_generated_secret():
    config = render("compose.yaml", "deploy/compose.test.yaml")
    fixture = REPO / "deploy" / "test-fixtures" / "halden_secret.txt"
    assert Path(config["secrets"]["halden_secret"]["file"]) == fixture
    assert fixture.read_text(encoding="utf-8") == DEMO_DEFAULT  # the simulator's published fictional value


def test_mi2_secret_files_stay_untracked():
    tracked = subprocess.run(["git", "ls-files", "deploy/secrets"], cwd=REPO, check=True, capture_output=True,
                             text=True).stdout
    assert tracked == ""


def _run_entrypoint(secret_file: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    probe = tmp_path / "probe.sh"
    # The wrapped command reports only whether HALDEN_SECRET equals the file content (never the value).
    probe.write_text('#!/bin/sh\n[ "$HALDEN_SECRET" = "$(cat "$EXPECTED")" ] && echo MATCH || echo MISMATCH\n')
    probe.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "HALDEN_SECRET_FILE": str(secret_file), "EXPECTED": str(secret_file)}
    return subprocess.run(["/bin/sh", str(ENTRYPOINT), str(probe)], capture_output=True, text=True, env=env,
                          check=False)


def test_mi2_entrypoint_exports_the_mounted_secret(tmp_path):
    secret = tmp_path / "halden_secret"
    value = os.urandom(24).hex()
    secret.write_text(value + "\n", encoding="utf-8")  # trailing newline is stripped, as the app's read_secret does
    done = _run_entrypoint(secret, tmp_path)
    assert done.returncode == 0 and done.stdout.strip() == "MATCH", done.stderr
    assert value not in done.stdout + done.stderr


@pytest.mark.parametrize("content", [None, "", "\n"])
def test_mi2_entrypoint_refuses_to_start_without_a_secret(tmp_path, content):
    secret = tmp_path / "halden_secret"
    if content is not None:
        secret.write_text(content, encoding="utf-8")
    done = _run_entrypoint(secret, tmp_path)
    assert done.returncode == 78, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""  # the wrapped command never ran (no fallback to the built-in default)
    assert "refusing to start" in done.stderr
