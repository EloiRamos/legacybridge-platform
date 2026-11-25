"""Customer search rules C-1 (ambiguity groups) and C-2 (parties only from the curated crosswalk)."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from uuid import UUID

from lbf.domain.enums import CreditHold
from lbf.domain.models import BackendAccount, CustomerSearchResult, DataIssue, NativeRef, Party

LEGAL_SUFFIXES = frozenset(
    {"ltd", "limited", "gmbh", "ag", "ab", "as", "asa", "oy", "oyj", "aps", "sl", "s l", "plc", "bv", "nv"}
)


def name_stem(name: str) -> str:
    """Normalized name stem: case- and accent-folded, punctuation removed, legal suffixes dropped.

    Used only to *flag* ambiguity (C-1). It never merges accounts.
    """
    text = unicodedata.normalize("NFKD", name.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    words = text.split()
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if len(words) >= 2 and " ".join(words[-2:]) in LEGAL_SUFFIXES:
        words = words[:-2]
    return " ".join(words)


def ambiguity_groups(accounts: Iterable[BackendAccount]) -> list[list[NativeRef]]:
    """C-1: a group forms when >= 2 accounts **in the same backend** share a name stem."""
    buckets: dict[tuple[str, str], list[NativeRef]] = defaultdict(list)
    for account in accounts:
        buckets[(account.ref.backend_id, name_stem(account.name_raw))].append(account.ref)
    groups = [sorted(refs, key=lambda r: r.native_id) for refs in buckets.values() if len(refs) >= 2]
    return sorted(groups, key=lambda g: (g[0].backend_id, g[0].native_id))


def build_parties(
    accounts: Iterable[BackendAccount],
    crosswalk: Mapping[tuple[str, str], tuple[UUID, str]],
    party_members: Mapping[UUID, list[BackendAccount]],
) -> list[Party]:
    """C-2: a party is formed **only** from curated crosswalk rows.

    ``crosswalk`` maps ``(backend_id, native_id)`` to ``(party_id, display_name)``;
    ``party_members`` gives every account of a party (so a search hit in one backend shows the
    linked account in the other). Name similarity is never consulted here.
    """
    seen: dict[UUID, Party] = {}
    for account in accounts:
        key = (account.ref.backend_id, account.ref.native_id)
        if key not in crosswalk:
            continue
        party_id, display = crosswalk[key]
        if party_id in seen:
            continue
        members = party_members.get(party_id, [account])
        holds = {m.credit_hold for m in members}
        conflicts = []
        if CreditHold.HOLD in holds and len(holds) > 1:
            conflicts.append(
                DataIssue(
                    code="CROSS_BACKEND_HOLD_CONFLICT",
                    field="credit_hold",
                    raw_value=", ".join(f"{m.ref.backend_id}:{m.ref.native_id}={m.credit_hold}" for m in members),
                    message="Credit hold differs between linked accounts: "
                    + "; ".join(f"{m.ref.backend_id.upper()} {m.ref.native_id} {m.credit_hold}" for m in members),
                )
            )
        seen[party_id] = Party(party_id=party_id, display_name=display, accounts=members, conflicts=conflicts)
    return sorted(seen.values(), key=lambda p: p.display_name)


def search_result(
    accounts: list[BackendAccount],
    crosswalk: Mapping[tuple[str, str], tuple[UUID, str]],
    party_members: Mapping[UUID, list[BackendAccount]],
) -> CustomerSearchResult:
    ordered = sorted(accounts, key=lambda a: (a.ref.backend_id, a.ref.native_id))
    return CustomerSearchResult(
        accounts=ordered,
        parties=build_parties(ordered, crosswalk, party_members),
        ambiguity_groups=ambiguity_groups(ordered),
    )
