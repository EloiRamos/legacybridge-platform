"""The repository secret scanner (``acceptance/secret_scan.py``, ``make secret-scan``) detects
``OPENAI_API_KEY`` assignments carrying a literal value even when the value lacks the ``sk-`` prefix,
without flagging references, empty values, placeholders or documentation examples.

Fixtures are assembled at run time (the variable name is split) so this file never contains a scannable assignment
itself; no value below is a real credential.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
NAME = "OPENAI_API" + "_KEY"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("lbf_secret_scan_under_test", REPO / "acceptance" / "secret_scan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()
RULE = "openai-key-assignment"

POSITIVE = [
    f"{NAME}=abcd1234efgh5678",                 # direct, non-sk value
    f'{NAME}="Zq9-nonstandard_value-123"',      # double-quoted
    f"{NAME}='single-quoted-value-42'",         # single-quoted
    f"{NAME} = spaced_value_998877",            # whitespace around '='
    f"{NAME}=\tTabbedValue12345",               # tab after '='
    f"export {NAME}=exported1234567",           # shell export
    f'"{NAME}": "jsonvalue12345678"',           # JSON
    f"  {NAME}: yamlvalue12345678",             # YAML
    f"{NAME}=sk-proj-" + "a" * 24,              # conventional sk- key (also the openai-key rule)
]

NEGATIVE = [
    f"{NAME}=",                                  # empty (as in .env.example templates)
    f"{NAME}=\n",
    f"{NAME}_FILE=/run/secrets/openai_api_key",  # the Docker-secret path variable
    f"{NAME}=${{{NAME}}}",                       # compose/shell reference
    f"{NAME}=$OPENAI_KEY_FROM_VAULT",            # shell variable
    f"{NAME}=<your-openai-key>",                 # documentation placeholder
    f"{NAME}=your-key-here",
    f"{NAME}=changeme-please",
    f"{NAME}=REDACTED_VALUE",
    f"{NAME}=xxxxxxxxxxxxxxxx",
    f"{NAME}=placeholder-value",
    f"{NAME}=example-key-value",
    f"{NAME}=test-key-not-a-secret",             # the repository's documented test placeholder
    f"{NAME}=sk-...",                            # documentation example
    f"{NAME}=short",                             # too short to be a key
    f"env -u {NAME} pytest -q",                  # unsetting, no assignment
    f'os.environ.get("{NAME}")',                 # read, no assignment
]


def _rule_hits(text: str) -> list[bytes]:
    return [m.group(0) for m in scanner.PATTERNS[RULE].finditer(text.encode())]


# Neutral test ids: the fixture text must not appear in pytest output (junit, logs), which the acceptance run scans
# for secrets (SEC-10 over captured outputs).
@pytest.mark.parametrize("text", POSITIVE, ids=[f"positive-{i}" for i in range(len(POSITIVE))])
def test_mi4_assignment_with_a_literal_value_is_detected(text):
    assert _rule_hits(text), POSITIVE.index(text)


@pytest.mark.parametrize("text", NEGATIVE, ids=[f"negative-{i}" for i in range(len(NEGATIVE))])
def test_mi4_references_placeholders_and_doc_examples_are_not_flagged(text):
    assert _rule_hits(text) == [], NEGATIVE.index(text)


def test_mi4_scan_reports_the_rule_without_printing_the_value(tmp_path):
    value = "nonstandard" + "KeyValue" + "4711"
    f = tmp_path / "leak.env"
    f.write_text(f"LBF_PROFILE=demo\n{NAME}={value}\n", encoding="utf-8")
    findings, scanned = scanner.scan([f])
    assert scanned == 1
    hits = [x for x in findings if x["rule"] == RULE]
    assert len(hits) == 1 and hits[0]["line"] == 2
    assert value not in repr(findings)  # findings carry a hash prefix, never the match


def test_mi4_clean_env_template_has_no_findings(tmp_path):
    f = tmp_path / "template.env"
    f.write_text(f"{NAME}=\nOPENAI_MODEL=\n{NAME}_FILE=/run/secrets/openai_api_key\n", encoding="utf-8")
    findings, _ = scanner.scan([f])
    assert [x for x in findings if x["rule"] == RULE] == []
