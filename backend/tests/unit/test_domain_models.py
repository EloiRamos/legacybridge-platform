"""Domain models, evidence mapping, correlation values, inventory and customer rules."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from lbf.domain import correlation, customers, inventory
from lbf.domain.enums import (
    AvailabilityStatus,
    CreditHold,
    DraftOrigin,
    EvidenceClass,
    Outcome,
    ReconciliationVerdict,
    VerificationLevel,
)
from lbf.domain.errors import HTTP_STATUS, ErrorCode, IntegrationError
from lbf.domain.evidence import EVIDENCE_OUTCOME, OUTCOME_EVIDENCE, verification_level
from lbf.domain.models import (
    Availability,
    BackendAccount,
    DraftContent,
    DraftContentLine,
    DraftVersion,
    NativeRef,
    ReconciliationResult,
    RecordedOutcome,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _content(**kw):
    base = {
        "backend_id": "halden",
        "native_customer_id": "00417",
        "lines": [
            DraftContentLine(
                line_no=1,
                canonical_sku="BW-1001",
                native_item="BW-1001",
                quantity_base=10,
                quantity_native=10,
                native_uom="EA",
                unit_price_estimate=Decimal("4.85"),
                line_estimate=Decimal("48.50"),
            )
        ],
    }
    base.update(kw)
    return DraftContent(**base)


# ---------------------------------------------------------------- evidence (closed sets)


def test_every_evidence_class_maps_to_exactly_one_outcome():
    assert set(EVIDENCE_OUTCOME) == set(EvidenceClass)
    total = sum(len(v) for v in OUTCOME_EVIDENCE.values())
    assert total == len(EvidenceClass)


@given(outcome=st.sampled_from(list(Outcome)), evidence=st.sampled_from(list(EvidenceClass)))
def test_recorded_outcome_validator_is_closed(outcome, evidence):
    ok = evidence in OUTCOME_EVIDENCE[outcome]
    kwargs = {"outcome": outcome, "evidence": evidence, "backend_order_ref": "SO-1"}
    if ok:
        RecordedOutcome(**kwargs)
    else:
        with pytest.raises(ValidationError):
            RecordedOutcome(**kwargs)


def test_confirmed_requires_order_ref():
    with pytest.raises(ValidationError):
        RecordedOutcome(outcome=Outcome.CONFIRMED, evidence=EvidenceClass.READ_BACK)


def test_verification_levels():
    assert verification_level(Outcome.CONFIRMED, EvidenceClass.READ_BACK) is VerificationLevel.VERIFIED_BY_READ
    assert verification_level(Outcome.CONFIRMED, EvidenceClass.ACKNOWLEDGED_ONLY) is VerificationLevel.ACKNOWLEDGED_ONLY
    assert verification_level(Outcome.CLOSED_ATTESTED, EvidenceClass.ATTESTED_BOOKED) is VerificationLevel.ATTESTED
    assert verification_level(Outcome.UNKNOWN, EvidenceClass.INTERRUPTED) is VerificationLevel.UNRESOLVED
    with pytest.raises(ValueError):
        verification_level(Outcome.NOT_APPLIED, EvidenceClass.READ_BACK)


# ---------------------------------------------------------------- reconciliation rules R-1


def test_absence_only_with_not_applied_evidence():
    ReconciliationResult(
        verdict=ReconciliationVerdict.NO_MATCH, absence_proven=True, absence_evidence=EvidenceClass.NEVER_DELIVERED
    )
    with pytest.raises(ValidationError):
        ReconciliationResult(
            verdict=ReconciliationVerdict.NO_MATCH, absence_proven=True, absence_evidence=EvidenceClass.READ_BACK
        )
    with pytest.raises(ValidationError):
        ReconciliationResult(verdict=ReconciliationVerdict.NO_MATCH, absence_proven=True)
    with pytest.raises(ValidationError):
        ReconciliationResult(verdict=ReconciliationVerdict.MATCHED)
    with pytest.raises(ValidationError):
        ReconciliationResult(verdict=ReconciliationVerdict.AMBIGUOUS, matched_order_ref="SO-1")


# ---------------------------------------------------------------- drafts and hashes


def test_draft_version_hash_binds_content_and_version():
    op = uuid4()
    v1 = DraftVersion.build(
        operation_id=op, version=1, content=_content(), created_by=uuid4(), origin=DraftOrigin.UI, created_at=NOW
    )
    with pytest.raises(ValidationError):
        DraftVersion(**{**v1.model_dump(), "version": 2})
    v2 = DraftVersion.build(
        operation_id=op, version=2, content=_content(), created_by=uuid4(), origin=DraftOrigin.UI, created_at=NOW
    )
    assert v1.content_hash != v2.content_hash


def test_draft_content_rules():
    with pytest.raises(ValidationError):
        _content(extra_field=1)
    with pytest.raises(ValidationError):
        _content(lines=[])
    line = _content().lines[0]
    with pytest.raises(ValidationError):
        _content(lines=[line.model_copy(update={"line_no": 2})])
    with pytest.raises(ValidationError):
        DraftContentLine(line_no=1, native_item="X", quantity_base=1000, quantity_native=1, native_uom="EA")
    assert _content().estimated_total() == Decimal("48.50")
    no_price = line.model_copy(update={"line_estimate": None})
    assert _content(lines=[no_price]).estimated_total() is None


def test_money_serializes_as_quantized_string():
    dumped = _content().model_dump(mode="json")
    assert dumped["lines"][0]["line_estimate"] == "48.50"


# ---------------------------------------------------------------- availability


def test_availability_quantity_rule():
    base = {
        "canonical_sku": "BW-1001",
        "backend_id": "skarvik",
        "native_item": "X",
        "as_of": NOW,
        "freshness": "SNAPSHOT",
        "explanation": "e",
    }
    Availability(status=AvailabilityStatus.STALE, available_base=None, **base)
    with pytest.raises(ValidationError):
        Availability(status=AvailabilityStatus.STALE, available_base=5, **base)
    with pytest.raises(ValidationError):
        Availability(status=AvailabilityStatus.IN_STOCK, available_base=None, **base)


def test_conversion_rules_i2():
    assert inventory.convert_to_base(35, 10, 1).available_base == 350
    assert inventory.convert_to_base(5, 52, 5).available_base == 52
    assert inventory.convert_to_base(3, 52, 5).issue == "NON_INTEGRAL_CONVERSION"
    assert inventory.convert_to_base(3, None, None).issue == "MISSING_CONVERSION_FACTOR"
    assert inventory.quantity_to_native(30, 10, 1) == 3
    assert inventory.quantity_to_native(31, 10, 1) is None
    assert inventory.is_stale(datetime(2026, 11, 21, 11, 59, 29, tzinfo=UTC), NOW, 30)
    assert not inventory.is_stale(datetime(2026, 11, 21, 11, 59, 30, tzinfo=UTC), NOW, 30)


# ---------------------------------------------------------------- correlation (ADR-F05)


@given(st.integers(min_value=0, max_value=2**50 - 1))
def test_correlation_values_are_12_chars_crockford(bits):
    value = correlation.new_correlation_value(lambda n: bits)
    assert len(value) == 12 and correlation.is_correlation_value(value)
    assert len(correlation.message_id(value, 1)) == 15


def test_correlation_excludes_ambiguous_letters():
    assert not set("ILOU") & set(correlation.CROCKFORD_ALPHABET)
    with pytest.raises(ValueError):
        correlation.message_id("LBIIIIIIIIII", 1)


# ---------------------------------------------------------------- customers C-1/C-2


def _acct(backend, nid, name, hold=CreditHold.CLEAR):
    return BackendAccount(
        ref=NativeRef(backend_id=backend, native_id=nid), name_raw=name, name_display=name, credit_hold=hold
    )


def test_c1_ambiguity_within_backend_only():
    accts = [
        _acct("halden", "10233", "Alder & Finch Fabrication"),
        _acct("halden", "10871", "ALDER & FINCH FABRICATION LTD", CreditHold.HOLD),
        _acct("skarvik", "KND-0150", "Göta Hamnservice AB"),
        _acct("skarvik", "KND-0151", "GÖTA HAMNSERVICE AB"),
        _acct("halden", "00417", "NORDKAP MARINE SERVICES AS"),
        _acct("skarvik", "KND-0081", "Nordkap Marine Services AS"),
    ]
    groups = customers.ambiguity_groups(accts)
    as_ids = [[r.native_id for r in g] for g in groups]
    assert ["10233", "10871"] in as_ids and ["KND-0150", "KND-0151"] in as_ids
    assert not any("00417" in g for g in as_ids)  # cross-backend similarity never groups


def test_c2_parties_only_from_crosswalk():
    pid = UUID(int=1)
    nordkap_h = _acct("halden", "00417", "NORDKAP MARINE SERVICES AS")
    nordkap_s = _acct("skarvik", "KND-0081", "Nordkap Marine Services AS")
    alder = _acct("halden", "10233", "Alder & Finch Fabrication")
    crosswalk = {
        ("halden", "00417"): (pid, "Nordkap Marine Services AS"),
        ("skarvik", "KND-0081"): (pid, "Nordkap Marine Services AS"),
    }
    result = customers.search_result([nordkap_h, nordkap_s, alder], crosswalk, {pid: [nordkap_h, nordkap_s]})
    assert len(result.parties) == 1 and len(result.parties[0].accounts) == 2
    no_crosswalk = customers.search_result([nordkap_h, nordkap_s], {}, {})
    assert no_crosswalk.parties == []


def test_cross_backend_hold_conflict_is_reported():
    pid = UUID(int=2)
    h = _acct("halden", "04450", "Vantaa Cold Storage Oy", CreditHold.HOLD)
    s = _acct("skarvik", "KND-0114", "Vantaa Cold Storage Oy", CreditHold.CLEAR)
    result = customers.search_result(
        [s], {("skarvik", "KND-0114"): (pid, "Vantaa"), ("halden", "04450"): (pid, "Vantaa")}, {pid: [h, s]}
    )
    assert result.parties[0].conflicts[0].code == "CROSS_BACKEND_HOLD_CONFLICT"


# ---------------------------------------------------------------- errors


def test_error_taxonomy_http_codes():
    assert set(HTTP_STATUS) == set(ErrorCode)
    assert HTTP_STATUS[ErrorCode.CONFLICT_OVERLAP] == 409
    assert HTTP_STATUS[ErrorCode.REAUTH_REQUIRED] == 401
    with pytest.raises(ValueError):
        IntegrationError(ErrorCode.FORBIDDEN)
