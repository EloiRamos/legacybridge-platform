"""Import-boundary tests B-1 … B-9.

Each rule scans the source tree with ``ast`` and applies to whatever packages exist at the time of the
run (the rules do not change when packages are added).

B-7 is defined here: it makes the two "Must not" import rules that had no
identifier testable — (a) the assistant package may import, among workflow write commands, only
``create_draft``/``revise_draft`` and never engines, adapters, persistence writers or demo controls;
(b) ``lbf.demo`` is imported by no production package except the API application factory.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
SRC = BACKEND / "src" / "lbf"
V1BRIDGE = SRC / "adapters" / "halden" / "v1bridge"


def py_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py") if ".venv" not in p.parts)


def imports_of(path: Path) -> list[tuple[str, list[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, []) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, [alias.name for alias in node.names]))
    return found


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def offenders(files: list[Path], pred) -> list[str]:
    bad = []
    for f in files:
        for mod, names in imports_of(f):
            if pred(mod, names):
                bad.append(f"{f.relative_to(REPO)}: {mod} {names}")
    return bad


FRAMEWORKS = ("fastapi", "starlette", "sqlalchemy", "httpx", "httpx2", "openai", "psycopg", "alembic", "uvicorn")


def _is(mod: str, roots: tuple[str, ...]) -> bool:
    return any(mod == r or mod.startswith(r + ".") for r in roots)


def test_b1_domain_and_ports_are_framework_neutral():
    files = py_files(SRC / "domain") + py_files(SRC / "ports")
    assert files, "domain package missing"
    assert offenders(files, lambda m, n: _is(m, FRAMEWORKS)) == []


def test_b2_skarvik_simulator_never_imports_lbf():
    files = py_files(REPO / "legacy-systems" / "skarvik-oes")
    assert offenders(files, lambda m, n: _is(m, ("lbf", "lbf_oracles"))) == []


def test_b3_oracles_never_import_lbf():
    files = py_files(REPO / "oracles") + py_files(REPO / "evals" / "oracles")
    assert files, "oracles package missing"
    assert offenders(files, lambda m, n: _is(m, ("lbf",))) == []


def test_b3_wiretap_is_an_independent_observer():
    """The wire-tap is wire truth only if it shares nothing with the application."""
    files = py_files(REPO / "tools" / "wiretap")
    assert files, "tools/wiretap missing"
    assert offenders(files, lambda m, n: _is(m, ("lbf", "lbf_oracles", "skarvik"))) == []


def test_b3_offline_fakes_are_never_imported_by_production_code():
    fakes = ("lbf.adapters.halden.fake", "lbf.adapters.skarvik.fake")
    files = [f for f in py_files(SRC) if module_name(f) not in fakes]
    assert offenders(files, lambda m, n: _is(m, fakes) or (m in ("lbf.adapters.halden", "lbf.adapters.skarvik")
                                                         and "fake" in n)) == []


def test_b4_sim_control_client_only_in_demo_and_tests():
    bad = []
    for f in py_files(SRC):
        if V1BRIDGE in f.parents or (SRC / "demo") in f.parents:
            continue
        if "SimControlClient" in f.read_text(encoding="utf-8"):
            bad.append(str(f.relative_to(REPO)))
    assert bad == []


def test_b5_application_never_references_oracle_endpoints():
    bad = [str(f.relative_to(REPO)) for f in py_files(SRC) if "/_sim/oracle" in f.read_text(encoding="utf-8")]
    assert bad == []


WRITE_CALL = re.compile(r"\.(handoff|redeliver|withdraw_unclaimed)\(")
B6_ALLOWED = {"lbf.engines.executor", "lbf.workflow.withdraw_unclaimed"}


def test_b6_write_handoffs_only_from_executor_and_withdraw_handler():
    bad = []
    for f in py_files(SRC):
        mod = module_name(f)
        if mod.startswith("lbf.adapters") or mod in B6_ALLOWED:
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if WRITE_CALL.search(code) and not code.lstrip().startswith("def "):
                bad.append(f"{f.relative_to(REPO)}:{lineno}")
    assert bad == []


ASSISTANT_ALLOWED_COMMANDS = {"create_draft", "revise_draft"}
ASSISTANT_FORBIDDEN_ROOTS = (
    "lbf.engines",
    "lbf.adapters",
    "lbf.demo",
    "lbf.persistence.audit",
    "lbf.workflow.executor",
)


def test_b7a_assistant_imports_only_draft_commands():
    files = py_files(SRC / "assistant")

    def pred(mod: str, names: list[str]) -> bool:
        if _is(mod, ASSISTANT_FORBIDDEN_ROOTS):
            return True
        if mod == "lbf.workflow.commands":
            return not names or not set(names) <= ASSISTANT_ALLOWED_COMMANDS
        if mod == "lbf.workflow" and "commands" in names:
            return True
        return False

    assert offenders(files, pred) == []


def test_b7b_demo_package_imported_only_by_api_app_factory():
    bad = []
    for f in py_files(SRC):
        mod = module_name(f)
        if mod.startswith("lbf.demo") or mod == "lbf.api.app":
            continue
        bad += offenders([f], lambda m, n: _is(m, ("lbf.demo",)))
    assert bad == []


def test_b8_openai_sdk_only_in_provider_module():
    allowed = SRC / "assistant" / "providers" / "openai_provider.py"
    files = [f for f in py_files(SRC) if f != allowed]
    assert offenders(files, lambda m, n: _is(m, ("openai",))) == []
    neutral = (
        py_files(SRC / "domain") + py_files(SRC / "workflow") + py_files(SRC / "persistence") + py_files(SRC / "api")
    )
    assert offenders(neutral, lambda m, n: _is(m, ("lbf.assistant.provider", "lbf.assistant.providers"))) == []


FORBIDDEN_LOCK_PACKAGES = re.compile(
    r"^(anthropic|langchain.*|langgraph.*|llama[-_]index.*|google-generativeai|google-genai|mistralai|cohere|"
    r"litellm|openai-agents|groq|together)$"
)


@pytest.mark.parametrize("lock", ["backend/uv.lock"])
def test_b9_no_other_provider_sdk_or_langchain_in_lock(lock):
    data = tomllib.loads((REPO / lock).read_text(encoding="utf-8"))
    names = {p["name"] for p in data.get("package", [])}
    assert {n for n in names if FORBIDDEN_LOCK_PACKAGES.match(n)} == set()
