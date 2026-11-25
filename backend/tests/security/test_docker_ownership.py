"""The Docker tooling proves ownership before it changes or removes anything. Runs against a stub ``docker`` that holds
an inventory and records every call: no daemon is used and no real resource is touched."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LABEL = "io.legacybridge.isolated.owner"
PROJECT = "com.docker.compose.project"

STUB = '''#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["STUB_STATE"]))
a = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write(" ".join(a) + "\\n")
if state.get("down"):
    sys.exit(1)
opt = {x.split("=", 1)[0] for x in a}
def flt(kind):
    f = [x for x in a if x.startswith(("label=", "volume="))]
    out = []
    for r in state[kind]:
        ok = True
        for x in f:
            k, v = x.split("=", 1)
            if k == "label":
                key, val = v.split("=", 1)
                ok = ok and r["labels"].get(key) == val
            else:
                ok = ok and v in r.get("mounts", [])
        if ok:
            out.append(r)
    return out
kinds = {"ps": "container", "volume": "volume", "network": "network", "image": "image"}
if a[0] == "ps":
    for r in flt("container"):
        print(r["id"])
elif a[1:2] == ["ls"]:
    for r in flt(a[0]):
        print(f"{r['id']} {r.get('name', r['id'])}" if "{{.ID}} {{.Name}}" in a else r["id"])
elif a[1:2] == ["inspect"]:
    r = next((r for r in state[a[0]] if r["id"] == a[-1]), None)
    if r is None or r.get("unreadable"):
        sys.exit(1)
    key = a[a.index("--format") + 1].split('"')[1]
    print(r["labels"].get(key, ""))
'''


@pytest.fixture
def box(tmp_path):
    """A copy of the two scripts beside a stub docker; ``deploy/secrets`` is the copy's own."""
    root = tmp_path / "checkout"
    (root / "tools").mkdir(parents=True)
    (root / "deploy" / "secrets").mkdir(parents=True)
    for name in ("isolated.sh", "owned_project.sh"):
        shutil.copy2(REPO / "tools" / name, root / "tools" / name)
    (root / "deploy" / "make_secrets.sh").write_text("#!/bin/sh\n")
    (root / "deploy" / "make_secrets.sh").chmod(0o755)
    shutil.copy2(REPO / "deploy" / "restore.sh", root / "deploy" / "restore.sh")
    shutil.copy2(REPO / "deploy" / "backup.sh", root / "deploy" / "backup.sh")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(STUB)
    (bin_ / "docker").chmod(0o755)

    class Box:
        state = {"container": [], "volume": [], "network": [], "image": []}  # noqa: RUF012
        path = root
        token = "a" * 32

        def own(self, ns="lbf-t1"):
            (root / "deploy/secrets" / f"isolated-{ns}.owner").write_text(self.token)
            (root / "deploy/secrets" / f"{ns}-credentials.txt").write_text("x")

        def run(self, *cmd, ns="lbf-t1", extra=None):
            (tmp_path / "state.json").write_text(json.dumps(self.state))
            log = tmp_path / "log"
            log.write_text("")
            env = {"PATH": f"{bin_}:{os.environ['PATH']}", "LBF_NS": ns, "STUB_STATE": str(tmp_path / "state.json"),
                   "LBF_EXCHANGE_SOURCE": str(tmp_path), "LBF_PAUSE_SOURCE": str(tmp_path), **(extra or {}),
                   "STUB_LOG": str(log), "TMPDIR": str(tmp_path), "HOME": str(tmp_path)}
            r = subprocess.run(list(cmd), cwd=root, env=env, capture_output=True, text=True, timeout=60)
            calls = log.read_text().splitlines()
            self.changes = [c for c in calls if not c.startswith("ps ") and c.split()[1] not in ("ls", "inspect")]
            return r

    b = Box()
    b.state = {"container": [], "volume": [], "network": [], "image": []}
    return b


def mine(box, ns="lbf-t1", owner=None):
    labels = {PROJECT: ns, LABEL: owner or box.token}
    box.state = {
        "container": [{"id": "c1", "labels": labels}], "network": [{"id": "n1", "name": f"{ns}-internal",
                                                                    "labels": labels}],
        "volume": [{"id": f"{ns}-pgdata", "labels": labels}], "image": [{"id": f"{ns}/app:local", "labels": labels}]}


def test_down_removes_exactly_what_this_checkout_created(box):
    mine(box)
    box.own()
    box.state["volume"].append({"id": "lbf-t1-pgdata-of-someone", "labels": {}})
    box.state["container"].append({"id": "c9", "labels": {PROJECT: "other"}})
    r = box.run("./tools/isolated.sh", "down")
    assert r.returncode == 0, r.stderr
    assert box.changes == ["rm -f -v c1", "network rm n1", "volume rm lbf-t1-pgdata", "image rm lbf-t1/app:local"]
    assert not list((box.path / "deploy/secrets").iterdir())


@pytest.mark.parametrize("kind", ["container", "volume", "network", "image"])
@pytest.mark.parametrize("command", ["up", "db-up", "down", "owned", "compose"])
@pytest.mark.parametrize("fault", ["foreign", "unlabelled", "unreadable"])
def test_one_foreign_resource_stops_everything(box, kind, command, fault):
    mine(box)
    box.own()
    target = box.state[kind][0]
    if fault == "unreadable":
        target["unreadable"] = True
    else:
        target["labels"] = {**target["labels"], LABEL: "b" * 32 if fault == "foreign" else ""}
    r = box.run("./tools/isolated.sh", command)
    assert r.returncode == 3 and "refusing" in r.stderr and box.changes == []
    assert (box.path / "deploy/secrets/isolated-lbf-t1.owner").exists()


@pytest.mark.parametrize("command", ["up", "db-up", "down", "fresh", "owned", "compose"])
def test_a_name_without_a_recorded_token_is_not_ownership(box, command):
    mine(box)
    r = box.run("./tools/isolated.sh", command)
    assert r.returncode == 3 and box.changes == []
    assert not (box.path / "deploy/secrets/isolated-lbf-t1.owner").exists()


def test_a_volume_that_only_shares_the_name_is_found(box):
    """A volume created by hand has no Compose label; it is matched by its exact name."""
    box.own()
    box.state["volume"] = [{"id": "lbf-t1-skarvik-data", "labels": {}}]
    r = box.run("./tools/isolated.sh", "down")
    assert r.returncode == 3 and "lbf-t1-skarvik-data" in r.stderr and box.changes == []


@pytest.mark.parametrize("command", ["up", "down", "fresh", "db-up", "owned", "compose"])
def test_an_unreadable_daemon_fails_closed(box, command):
    box.own()
    box.state["down"] = True
    r = box.run("./tools/isolated.sh", command)
    assert r.returncode == 3 and box.changes == []


@pytest.mark.parametrize("ns", ["lbf", "legacybridge-platform", "legacybridge-c2backup", "lbf-internal", "A", "a/b",
                                "a b", "-a", "a-", "../x", "a.b", "x;y"])
def test_reserved_and_malformed_names_are_refused(box, ns):
    r = box.run("./tools/isolated.sh", "down", ns=ns)
    assert r.returncode == 2 and box.changes == []


def test_an_empty_namespace_is_claimed_and_compose_gets_the_token(box):
    r = box.run("./tools/isolated.sh", "up")
    assert r.returncode == 0, r.stderr
    token = (box.path / "deploy/secrets/isolated-lbf-t1.owner").read_text()
    assert len(token) == 32 and any(" up " in f" {c} " for c in box.changes)


# ------------------------------------------------------------------------- the default deployment
def default(box, where=None):
    labels = {PROJECT: "legacybridge-platform", "com.docker.compose.project.working_dir": where or str(
        box.path.resolve())}
    box.state["container"] = [{"id": "db1", "labels": labels, "mounts": ["lbf-pgdata"]}]
    box.state["volume"] = [{"id": "lbf-pgdata", "labels": {PROJECT: "legacybridge-platform"}}]


def test_the_default_deployment_of_this_checkout_is_accepted(box):
    default(box)
    assert box.run("./tools/owned_project.sh").returncode == 0 and box.changes == []


@pytest.mark.parametrize("case", ["other-checkout", "foreign-volume", "outside-user", "no-container", "unreadable"])
def test_restore_stops_before_touching_resources_it_does_not_own(box, case):
    default(box)
    if case == "other-checkout":
        default(box, "/somewhere/else")
    elif case == "foreign-volume":
        box.state["volume"][0]["labels"] = {}
    elif case == "outside-user":
        box.state["container"].append({"id": "x1", "labels": {}, "mounts": ["lbf-pgdata"]})
    elif case == "no-container":
        box.state["container"] = []
    else:
        box.state["container"][0]["unreadable"] = True
    (box.path / "a.dump").write_text("x")
    r = box.run("./deploy/restore.sh", "a.dump", ns="")
    assert r.returncode == 3 and "nothing was changed" in r.stderr and box.changes == []


# ------------------------------------------------------------------------- the isolated campaign
def test_an_isolated_restore_replaces_only_its_own_volume(box):
    mine(box)
    box.own()
    box.state["volume"].append({"id": "lbf-pgdata", "labels": {PROJECT: "legacybridge-platform"}})
    (box.path / "a.dump").write_text("x")
    r = box.run("./deploy/restore.sh", "a.dump")
    assert r.returncode == 0, r.stderr
    assert "volume rm lbf-t1-pgdata" in box.changes and not any("lbf-pgdata" in c for c in box.changes)
    compose = [c for c in box.changes if c.startswith("compose ")]
    assert compose and all(c.startswith("compose -p lbf-t1 ") for c in compose)


@pytest.mark.parametrize("script", [("./deploy/restore.sh", "a.dump"), ("./deploy/backup.sh",)])
def test_an_isolated_backup_or_restore_refuses_a_foreign_stack(box, script):
    mine(box, owner="b" * 32)
    box.own()
    (box.path / "a.dump").write_text("x")
    r = box.run(*script)
    assert r.returncode == 3 and box.changes == []


def test_the_campaign_overlay_needs_its_own_directories(box):
    mine(box)
    box.own()
    ok = box.run("./tools/isolated.sh", "compose", "ps", extra={"LBF_CAMPAIGN": "1"})
    assert ok.returncode == 0 and "compose.campaign.yaml --profile test ps" in box.changes[0]
    bad = box.run("./tools/isolated.sh", "compose", "ps", extra={"LBF_CAMPAIGN": "1", "LBF_PAUSE_SOURCE": "./x"})
    assert bad.returncode == 2 and box.changes == []


def test_owned_needs_something_to_own(box):
    box.own()
    assert box.run("./tools/isolated.sh", "owned").returncode == 3


@pytest.mark.parametrize("ns", ["", "lbf-t1"])
def test_the_harness_proves_ownership_before_it_resets_or_kills(box, ns):
    """Run in the copied checkout, where nothing is owned: no reset, no kill, no Compose command."""
    for name in ("resilience/harness/stack.py", "resilience/harness/kill.py", "oracles"):
        src, dst = REPO / name, box.path / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
    mine(box, ns=ns or "legacybridge-platform")
    code = ("import subprocess\nfrom resilience.harness import kill, stack\n"
            "for f in (stack.Stack, kill.kill_app):\n"
            "    try:\n        f()\n    except subprocess.CalledProcessError as e:\n        print(e.returncode)\n")
    r = box.run(sys.executable, "-c", code, ns=ns)
    assert r.stdout.split() == ["3", "3"], r.stderr
    assert box.changes == []
