"""Static properties of the deployment files and tools: restart policies, and where the OpenAI key can and cannot go."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def load(name):
    return yaml.load((REPO / name).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 - a SafeLoader subclass


def test_a_service_never_restarts_without_the_database_it_needs():
    """After a daemon restart the application came back, its database did not, and it looped on the missing host."""
    services = load("compose.yaml")["services"]
    restarting = {n for n, s in services.items() if s.get("restart") == "unless-stopped"}
    assert restarting == {"app", "db", "halden", "skarvik"}


@pytest.mark.parametrize("overlay", ["deploy/compose.isolated.yaml", "deploy/compose.test.yaml"])
def test_disposable_stacks_mount_an_empty_key(overlay):
    source = load(overlay)["secrets"]["openai_api_key"]["file"]
    assert source == "./deploy/test-fixtures/openai_api_key.txt" and (REPO / source).read_bytes() == b""


def test_an_empty_key_file_is_no_key(monkeypatch):
    from lbf.assistant.config import read_key

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(REPO / "deploy/test-fixtures/openai_api_key.txt"))
    assert read_key() is None


def test_the_secret_scan_never_opens_the_openai_key(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "acceptance"))
    import secret_scan

    (tmp_path / "db_app.txt").write_text("database-password")
    (tmp_path / "openai_api_key.txt").write_text("k" * 40)
    real, opened = Path.read_text, []
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (opened.append(self.name), real(self, *a, **k))[1])
    monkeypatch.setattr(secret_scan, "SECRETS", tmp_path)
    assert [label for label, _ in secret_scan.secret_values()] == ["deploy/secrets/db_app.txt"]
    assert opened == ["db_app.txt"]
