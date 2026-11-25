"""Backend conformance suite CF-01 … CF-13.

One module, parametrized over backend harnesses. Every assertion is a business invariant expressed through the
port, the capability predicates and independent oracle views of backend state; no test branches on backend
identity.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from lbf.domain.enums import (
    AvailabilityStatus,
    EvidenceClass,
    ReconciliationVerdict,
    TransportObservationKind,
)
from lbf.domain.errors import ErrorCode, IntegrationError
from lbf.domain.evidence import NOT_APPLIED_EVIDENCE

from .harness import REPO, SimulatedCrash

K = TransportObservationKind
V = ReconciliationVerdict
EXPECTATIONS = yaml.safe_load((REPO / "acceptance" / "fixtures" / "inventory_expectations.yaml").read_text("utf-8"))


def _handoff(h, req):
    """The engine's order of operations (T09): read-only preflight (ticket assurance / share check), render, ONE
    handoff. The preflight verdict is not asserted here: business rejections must come from the backend itself."""
    h.port.preflight(req)
    return h.port.handoff(h.port.render(req))


def _accept_and_settle(h, req):
    started = h.now()
    obs = _handoff(h, req)
    h.settle()
    return obs, started


def _ack_for(h, req):
    acks = [a for a in h.port.poll_acknowledgements() if a.message_id == req.message_id]
    for a in acks:
        h.port.archive_acknowledgement(a)
    return acks


# --------------------------------------------------------------------------- CF-01


def test_cf01_render_is_deterministic(backend):
    req = backend.request()
    a, b = backend.port.render(req), backend.port.render(req)
    assert a.payload == b.payload and a.payload_sha256 == b.payload_sha256
    assert backend.port.render(backend.request(cv=req.correlation_value)).payload == a.payload


# --------------------------------------------------------------------------- CF-02


def test_cf02_exactly_one_handoff_per_call_and_never_raises(backend):
    req = backend.request()
    before = backend.handoffs(req)
    obs = _handoff(backend, req)
    assert obs.kind in set(K)
    backend.settle()
    assert backend.handoffs(req) - before == 1


# --------------------------------------------------------------------------- CF-03


def test_cf03_business_rejection_leaves_no_order(backend):
    req = backend.request(customer=backend.held_customer)
    obs, _ = _accept_and_settle(backend, req)
    if backend.caps().is_async:
        assert obs.kind is K.DELIVERED_PENDING_ACK
        acks = _ack_for(backend, req)
        assert [a.status for a in acks] == ["REJECTED"]
    else:
        assert obs.kind is K.REJECTED
    assert backend.orders(req) == []


# --------------------------------------------------------------------------- CF-04


def test_cf04_accepted_or_acknowledged_order_reconciles_to_same_ref(backend):
    req = backend.request()
    obs, started = _accept_and_settle(backend, req)
    if backend.caps().is_async:
        [ack] = _ack_for(backend, req)
        assert ack.status == "ACCEPTED"
        order_ref = ack.order_ref
    else:
        assert obs.kind is K.ACCEPTED
        order_ref = obs.backend_order_ref
    result = backend.port.reconcile(backend.query(req, started))
    assert result.verdict is V.MATCHED and result.matched_order_ref == order_ref
    assert backend.orders(req) == [order_ref]
    anchored = backend.port.reconcile(backend.query(req, started, anchor=order_ref))
    assert anchored.verdict is V.MATCHED and anchored.matched_order_ref == order_ref


# --------------------------------------------------------------------------- CF-05


def test_cf05_uncertain_handoff_with_committed_order_reconciles_without_second_handoff(backend):
    req = backend.request()
    backend.arm_uncertain_commit()
    obs, started = _accept_and_settle(backend, req)
    assert obs.kind in (K.UNCERTAIN, K.DELIVERED_PENDING_ACK)
    if backend.caps().is_async:
        assert _ack_for(backend, req) == []  # acknowledgement lost
    result = backend.port.reconcile(backend.query(req, started))
    assert result.verdict is V.MATCHED
    assert backend.handoffs(req) == 1 and len(backend.orders(req)) == 1


# --------------------------------------------------------------------------- CF-06


def test_cf06_absence_only_with_not_applied_evidence(backend):
    req = backend.request()

    def crash(_rendered):
        raise SimulatedCrash

    backend.port.before_publish = crash
    started = backend.now()
    with pytest.raises(SimulatedCrash):
        _handoff(backend, req)
    backend.port.before_publish = lambda _r: None
    backend.settle()
    result = backend.port.reconcile(backend.query(req, started))
    assert backend.handoffs(req) == 0 and backend.orders(req) == []
    if result.absence_proven:
        assert result.absence_evidence in NOT_APPLIED_EVIDENCE
    assert result.absence_proven is backend.caps().can_prove_absence_after_handoff
    if result.absence_proven:
        assert result.absence_evidence is EvidenceClass.NEVER_DELIVERED
    # and an accepted order never yields absence
    req2 = backend.request()
    _, started2 = _accept_and_settle(backend, req2)
    _ack_for(backend, req2) if backend.caps().is_async else None
    assert backend.port.reconcile(backend.query(req2, started2)).absence_proven is False


# --------------------------------------------------------------------------- CF-07


GATED = {
    "can_withdraw_before_pickup": [("withdraw_unclaimed", ("LB0000000000-01",))],
    "supports_redelivery": [("redeliver", None)],
    "is_async": [("poll_acknowledgements", ()), ("archive_acknowledgement", None)],
}


def test_cf07_capability_gated_methods(backend):
    caps = backend.caps()
    rendered = backend.port.render(backend.request())
    for predicate, methods in GATED.items():
        declared = getattr(caps, predicate)
        for name, args in methods:
            if args is None:
                args = (rendered,) if name == "redeliver" else (None,)
            if declared:
                continue  # supported: exercised by CF-13 / CF-03 / CF-04
            before = backend.io_count()
            with pytest.raises(IntegrationError) as exc:
                getattr(backend.port, name)(*args)
            assert exc.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
            assert backend.io_count() == before


# --------------------------------------------------------------------------- CF-08


def test_cf08_reads_never_write(backend):
    req = backend.request()
    _accept_and_settle(backend, req)
    before = backend.state_digest()
    port = backend.port
    port.health()
    port.search_customers("a")
    port.get_customer(backend.good_customer)
    port.list_orders(backend.good_customer)
    port.catalogue()
    port.stock()
    port.preflight(req)
    port.reconcile(backend.query(req, backend.now()))
    if backend.caps().is_async:
        port.poll_acknowledgements()
    assert backend.state_digest() == before


# --------------------------------------------------------------------------- CF-09


def test_cf09_ambiguous_correlation_is_never_matched(backend):
    req = backend.request()
    started = backend.now()  # first handoff time: the freshness rule (S-I7) is relative to it
    backend.plant_duplicate(req)
    backend.settle()
    assert len(backend.orders(req)) == 2
    result = backend.port.reconcile(backend.query(req, started))
    assert result.verdict is V.AMBIGUOUS and result.matched_order_ref is None and len(result.candidates) == 2


# --------------------------------------------------------------------------- CF-10


def test_cf10_anchored_reconcile_never_matches_a_different_order(backend):
    first, second = backend.request(), backend.request(lines=[(backend.item, 3)])
    _accept_and_settle(backend, first)
    _, t2 = _accept_and_settle(backend, second)
    if backend.caps().is_async:
        _ack_for(backend, first)
        _ack_for(backend, second)
    [order_of_first] = backend.orders(first)
    result = backend.port.reconcile(backend.query(second, t2, anchor=order_of_first))
    assert result.verdict is not V.MATCHED and result.matched_order_ref is None


# --------------------------------------------------------------------------- CF-11


def test_cf11_availability_follows_rules(backend):
    snapshot = backend.port.stock()
    expected = EXPECTATIONS[snapshot.backend_id]
    by_item = {a.native_item: a for a in snapshot.availability}
    for row in expected:
        a = by_item[row["native_item"]]
        assert a.status.value == row["status"], row
        assert a.available_base == row["available_base"], row
        assert a.canonical_sku == row["canonical_sku"], row
        for code in row["issues"]:
            assert code in {i.code for i in a.issues}, row
    assert all(a.backend_id == snapshot.backend_id for a in snapshot.availability)  # I-3: never merged
    if backend.caps().inventory_freshness == "SNAPSHOT":
        backend.make_snapshots_stale((backend.caps().inventory_max_age_seconds or 0) + 5)
        stale = backend.port.stock()
        assert {a.status for a in stale.availability} == {AvailabilityStatus.STALE}
        assert all(a.available_base is None for a in stale.availability)


# --------------------------------------------------------------------------- CF-12


@pytest.mark.parametrize("query", ["alder", "göta", "nordkap", "vantaa"])
def test_cf12_search_never_merges_accounts(backend, query):
    accounts = backend.port.search_customers(query)
    refs = [(a.ref.backend_id, a.ref.native_id) for a in accounts]
    assert len(refs) == len(set(refs))
    assert all(a.ref.backend_id == backend.caps().backend_id for a in accounts)
    counts = Counter(r[1] for r in refs)
    assert all(v == 1 for v in counts.values())


# --------------------------------------------------------------------------- CF-13


def test_cf13_redelivery_within_window_creates_no_second_order(backend):
    req = backend.request()
    if not backend.caps().supports_redelivery:
        with pytest.raises(IntegrationError) as exc:
            backend.port.redeliver(backend.port.render(req))
        assert exc.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
        return
    backend.arm_uncertain_commit()  # first delivery processed, acknowledgement lost
    backend.port.preflight(req)
    rendered = backend.port.render(req)
    started = backend.now()
    backend.port.handoff(rendered)
    backend.settle()
    assert _ack_for(backend, req) == []
    obs = backend.port.redeliver(rendered)
    assert obs.kind is K.DELIVERED_PENDING_ACK
    backend.settle()
    [ack] = _ack_for(backend, req)
    assert ack.status == "DUPLICATE" and ack.order_ref == backend.orders(req)[0]
    assert len(backend.orders(req)) == 1 and backend.handoffs(req) == 2
    result = backend.port.reconcile(backend.query(req, started))
    assert result.verdict is V.MATCHED


def test_conformance_module_has_no_backend_identity_branches():
    """Expectations are capability predicates, never backend identity. Checked on the AST:
    (1) no string literal naming a backend or harness; (2) no branch condition (``if``/conditional expression/
    comprehension filter/``while``) that reads ``backend_id`` or the harness ``name``; (3) no ``isinstance``."""
    import ast

    tree = ast.parse(Path(__file__).read_text("utf-8"))
    problems: list[str] = []
    identity_words = ("halden", "skarvik")
    this = "test_conformance_module_has_no_backend_identity_branches"
    scanned = [n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name == this)]
    for node in (sub for top in scanned for sub in ast.walk(top)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(w in node.value.casefold() for w in identity_words):
                problems.append(f"line {node.lineno}: backend-identity literal {node.value!r}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance":
            problems.append(f"line {node.lineno}: isinstance call")
        tests: list[ast.expr] = []
        if isinstance(node, ast.If | ast.IfExp | ast.While):
            tests.append(node.test)
        if isinstance(node, ast.comprehension):
            tests.extend(node.ifs)
        for test in tests:
            for sub in ast.walk(test):
                if isinstance(sub, ast.Attribute) and sub.attr in ("backend_id", "name"):
                    problems.append(f"line {sub.lineno}: branch on .{sub.attr}")
    assert problems == []
