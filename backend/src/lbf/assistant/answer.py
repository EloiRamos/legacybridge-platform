"""Structured final answer and the grounding check.

A *fact* binds a value to one field of one tool result of this turn. The model names the field and copies the value;
the application compares them exactly and renders the claim from the **observed** value, so a fact shown to the
operator can never carry a value, polarity or field the system did not report. Free text (summary, warnings) is the
model's wording: it is checked, heuristically, for exact numbers and identifiers, for numbers attached to another
account, product, system or unit than the record they came from, for credit-hold polarity and for claims of actions
the assistant did not perform. It is *not* semantically verified, and the console says so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

HumanAction = Literal["open_operation", "submit_for_approval", "approve_in_ui", "recheck_in_ui", "escalate_in_ui",
                      "resolve_in_ui", "withdraw_unclaimed_in_ui", "authorize_redelivery_in_ui", "search_customers",
                      "check_inventory", "none"]
MAX_CLAIM = 400


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_call_id: str = Field(max_length=80, description="The tool_call_id value printed at the top of the cited "
                              "tool result.")
    field_path: str = Field(max_length=200, description="Path inside that tool result in dot/bracket notation, "
                            "e.g. accounts[0].native_id (no leading $ or /). It must name ONE field, never a whole "
                            "record.")


class Fact(BaseModel):
    """A fact as shown and stored. ``claim`` is rendered by the application when the model supplied a ``value``."""

    model_config = ConfigDict(extra="forbid")
    claim: str = Field(max_length=MAX_CLAIM)
    source: Source
    record: str | None = Field(default=None, max_length=200)


class AssistantAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=1200)
    facts: list[Fact] = Field(max_length=15)
    draft_operation_id: UUID | None
    warnings: list[str] = Field(max_length=10)
    next_human_actions: list[HumanAction] = Field(max_length=6)


class ModelFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=1000, description="The exact value of the cited field, copied character by "
                       "character. Write null for a null field and the number of entries for a list.")
    source: Source


class ModelAnswer(BaseModel):
    """What the model is asked to return (strict structured output)."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=1200)
    facts: list[ModelFact] = Field(max_length=15)
    draft_operation_id: UUID | None
    warnings: list[str] = Field(max_length=10)
    next_human_actions: list[HumanAction] = Field(max_length=6)


class _ParsedFact(BaseModel):
    """Either form: a copied ``value`` (model schema) or a prose ``claim`` (scripted providers, stored answers)."""

    model_config = ConfigDict(extra="forbid")
    claim: str | None = Field(default=None, max_length=MAX_CLAIM)
    value: str | None = Field(default=None, max_length=1000)
    source: Source

    @model_validator(mode="after")
    def _one(self) -> _ParsedFact:
        if self.claim is None and self.value is None:
            raise ValueError("a fact needs a value")
        return self


class _ParsedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=1200)
    facts: list[_ParsedFact] = Field(max_length=15)
    draft_operation_id: UUID | None
    warnings: list[str] = Field(max_length=10)
    next_human_actions: list[HumanAction] = Field(max_length=6)


def output_schema() -> dict[str, Any]:
    from lbf.assistant.tools import strict_schema

    return strict_schema(ModelAnswer)


@dataclass(frozen=True)
class Evidence:
    """What the turn may rely on besides the tool results. ``context_text`` (earlier turns) supports identifiers only,
    never quantities or states: those must come from this turn's results (stale conversational evidence)."""

    user_text: str = ""
    context_text: str = ""
    call_args: Mapping[str, Any] = field(default_factory=dict)
    receipts: int = 0


# --------------------------------------------------------------------------------------------------- field paths

MISSING = object()


def locate(value: Any, path: str) -> tuple[Any, list[dict[str, Any]]]:
    """(leaf or ``MISSING``, enclosing records outermost first)."""
    records: list[dict[str, Any]] = [value] if isinstance(value, dict) else []
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(value, list) or idx >= len(value):
                return MISSING, records
            value = value[idx]
        else:
            if not isinstance(value, dict) or part not in value:
                return MISSING, records
            value = value[part]
        if isinstance(value, dict):
            records.append(value)
    return value, records


def resolve(value: Any, path: str) -> Any:
    """``a.b[2].c`` style path into a JSON value; ``None`` when it does not resolve (or is null)."""
    found, _ = locate(value, path)
    return None if found is MISSING else found


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"untrusted_text"} and isinstance(value["untrusted_text"], str):
        return value["untrusted_text"]
    return value


def display(value: Any) -> str:
    """The observed value as text (also the form the model copies)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return str(len(value))
    return str(value)


def render(path: str, value: Any) -> str:
    """Deterministic claim for an observed field, e.g. ``credit_hold = HOLD``."""
    tail: list[str] = []
    for part in reversed(re.findall(r"[^.\[\]]+|\[\d+\]", path)):
        if part.startswith("["):
            if tail:
                break
            continue
        tail.insert(0, part)
    label = ".".join(tail) or "result"
    if value is None:
        text = f"{label} = null (not recorded)"
    elif isinstance(value, list):
        if value and all(isinstance(v, (str, int, float, bool)) for v in value):
            text = f"{label} = {', '.join(display(v) for v in value)}"
        else:
            text = f"{label}: {len(value)} {'entry' if len(value) == 1 else 'entries'}"
    else:
        text = f"{label} = {display(value)}"
    return text if len(text) <= MAX_CLAIM else text[: MAX_CLAIM - 1] + "…"


IDENTITY_KEYS = ("backend_id", "native_id", "native_customer_id", "native_item", "canonical_sku", "correlation_value",
                 "operation_id", "query")


def record_label(records: Iterable[dict[str, Any]]) -> str | None:
    """Which record a fact belongs to, from the innermost enclosing record that names itself."""
    for record in reversed(list(records)):
        parts = [str(record[k]).upper() if k == "backend_id" else str(record[k])
                 for k in ("backend_id", "native_id", "native_customer_id", "canonical_sku", "native_item",
                           "correlation_value")
                 if isinstance(record.get(k), str)]
        if parts:
            return " · ".join(dict.fromkeys(parts))[:200]
    return None


# --------------------------------------------------------------------------------------------------- exact tokens

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_TOKEN = re.compile(r"\w+(?:[-.:,/]\w+)*", re.UNICODE)
# The application's own constants: product names and the quantity rule the prompt and the gate state (1 to 999).
_CONSTANTS = re.compile(r"\b(?:HALDEN(?: ERP)? 3\.1|SKARVIK(?: OES)? 4|OES 4|ERP 3\.1)\b"
                        r"|\b1\s?(?:to|and|through|-|\u2013)\s?999\b", re.IGNORECASE)


def tokens(text: str) -> list[str]:
    """Digit-bearing tokens: whole identifiers, dates and numbers, never fragments of one."""
    return [m for m in _TOKEN.findall(_CONSTANTS.sub(" ", text)) if any(c.isdigit() for c in m)]


def _number(token: str) -> Decimal | None:
    plain = token.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token) else token.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", plain) or re.fullmatch(r"0\d+", plain):  # 00417 is an identifier
        return None
    try:
        return Decimal(plain)
    except InvalidOperation:
        return None


class TokenSet:
    def __init__(self, texts: Iterable[str]) -> None:
        self.raw: set[str] = set()
        self.numbers: set[Decimal] = set()
        self.dates: set[str] = set()
        for text in texts:
            for tok in tokens(text):
                self.add(tok)

    def add(self, token: str) -> None:
        self.raw.add(token.casefold())
        if (n := _number(token)) is not None:
            self.numbers.add(n)
        if re.match(r"\d{4}-\d{2}-\d{2}", token):
            self.dates.add(token[:10])
        if re.fullmatch(r"\d+-[a-z]+", token):  # 3-unit
            self.numbers.add(Decimal(token.split("-")[0]))

    def has(self, token: str) -> bool:
        low = token.casefold()
        if low in self.raw or low in self.dates:
            return True
        number = _number(token)
        if number is not None:
            return number in self.numbers
        if re.fullmatch(r"\d+-[a-z]+", low):
            return Decimal(low.split("-")[0]) in self.numbers
        pieces = [x for x in re.split(r"[:,/]", token) if any(c.isdigit() for c in x)]
        return len(pieces) > 1 and all(self.has(x) for x in pieces)


def leaves(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for v in value.values():
            yield from leaves(v)
    elif isinstance(value, list):
        yield str(len(value))
        for v in value:
            yield from leaves(v)
    elif value is not None and not isinstance(value, bool):
        yield str(value)


def _identity(records: Iterable[dict[str, Any]], args: Any) -> list[str]:
    out = []
    for record in [*records, args if isinstance(args, dict) else {}]:
        for key, value in record.items():
            if key in IDENTITY_KEYS and isinstance(value, str):
                out.append(value)
            elif key in ("names", "native_items") and isinstance(value, dict):
                out += [v for v in value.values() if isinstance(v, str)]
    return out


# --------------------------------------------------------------------------------------------------- polarity

_HOLD_CLEAR = re.compile(
    r"\b(?:not|no longer|isn't|is not|never)\b[^.;]{0,30}\b(?:hold|held)\b|\bwithout (?:a |any )?(?:credit )?hold\b"
    r"|\bno (?:active |current )?(?:credit )?hold\b"
    r"|\bhold\b[^.;,]{0,25}(?:\b(?:is|=|:)\s*|:\s*|=\s*)(?:no|false|none|off|clear(?:ed)?|inactive)\b"
    r"|\bcredit(?:[_ ]hold)? (?:status )?(?:is )?clear\b|\bhold (?:is )?clear\b"
    r"|\bclear(?:ed)? (?:to order|of (?:a |any )?(?:credit )?hold)\b",
    re.IGNORECASE)
_HOLD_SET = re.compile(
    r"\bon (?:a |active )?(?:credit[ -])?hold\b"
    r"|\bhold\b[^.;,]{0,25}(?:\bis\s+|:\s*|=\s*)(?:yes|true|hold|on|active|set)\b"
    r"|\bcredit[_ -]held\b", re.IGNORECASE)
_NEGATED_CLEAR = re.compile(r"\bnot (?:credit[- ])?clear|\b(?:not|never) (?:free (?:from|of)|without) (?:a |any )?"
                            r"(?:credit[- ])?hold\b", re.IGNORECASE)
_UNCERTAIN = re.compile(r"\b(?:whether|if|unknown|undetermined|cannot|can't|could not|unclear|unverified|no evidence"
                        r"|thinks?|claims?|said|says|colleague|believes?|asked|confirm)\b|\?", re.IGNORECASE)


def hold_polarity(text: str) -> str | None:
    """``HOLD`` / ``CLEAR`` asserted by one clause, or ``None`` when it asserts neither (or is hedged)."""
    if _UNCERTAIN.search(text):
        return None
    clear, held = _HOLD_CLEAR.search(text), _HOLD_SET.search(text)
    if _NEGATED_CLEAR.search(text):
        return "HOLD"
    if clear and re.search(r"\bnot\b|\bno\b|n't|\bnever\b|without", clear.group(0), re.IGNORECASE):
        return "CLEAR"  # "not on hold" also contains "on hold"
    if clear and held:
        return None
    return "CLEAR" if clear else "HOLD" if held else None


def _hold_records(value: Any) -> Iterable[tuple[str, set[str]]]:
    """(hold value, identifying words) of every record of a result that reports a credit hold."""
    if isinstance(value, dict):
        if isinstance(value.get("credit_hold"), str):
            words = {str(value[k]).casefold() for k in ("native_id", "backend_id", "name_display", "name_raw")
                     if isinstance(value.get(k), str)}
            yield value["credit_hold"], words
        for v in value.values():
            yield from _hold_records(v)
    elif isinstance(value, list):
        for v in value:
            yield from _hold_records(v)


def _clauses(text: str) -> list[str]:
    return [c for c in re.split(r"(?<=[.;!?])\s+|\n|, (?:while|but|whereas|and)\b| whereas | while | but ", text) if c]


def hold_contradiction(text: str, results: Mapping[str, Any]) -> bool:
    records = [r for payload in results.values() for r in _hold_records(payload)]
    for clause in _clauses(text):
        stated = hold_polarity(clause)
        if stated is None:
            continue
        if not records:
            return True  # a hold statement without any credit-hold observation in this turn
        low = clause.casefold()
        strong = [h for h, words in records if any(w in low for w in words if w not in ("halden", "skarvik"))]
        weak = [h for h, words in records if any(w in low for w in words if w in ("halden", "skarvik"))]
        observed = set(strong or weak or [h for h, _ in records])
        if stated not in observed:
            return True
        if len(observed) > 1 and not strong and not weak:
            return True  # accounts differ and the clause does not say which one it means
    return False


_ACTED = re.compile(
    r"\b(?:I|I've|I have|we|we've|the assistant|assistant)\s+(?:have |has |just |now |already )*"
    r"(approved|submitted|sent|transmitted|redelivered|withdrew|withdrawn|escalated|resolved|declined|re-?checked)\b",
    re.IGNORECASE)
_PASSIVE = re.compile(r"\b(?:was|has been|is now|have been|were)\s+(?:successfully |now )?(approved|submitted)\b",
                      re.IGNORECASE)
_DRAFTED = re.compile(
    r"\bdraft(?: order| version)?\b[^.;]{0,60}\b(?:was|has been|is|were)\s+(?:successfully |now )?"
    r"(?:created|prepared|revised|updated)\b"
    r"|\b(?:I|I've|I have)\s+(?:successfully )?(?:created|prepared|revised|updated)\b"
    r"|^(?:created|prepared|revised|updated) (?:a |the |one )?(?:\w+ )?draft\b", re.IGNORECASE)
_NEG = re.compile(r"\b(?:not|no|never|nothing|without|unless|until|before|if|once|when|cannot|failed|rejected"
                  r"|refused)\b|n't", re.IGNORECASE)


def action_overclaim(text: str, receipts: int, results: Mapping[str, Any]) -> bool:
    reported = " ".join(x for r in results.values() for x in leaves(r)).casefold()
    for clause in _clauses(text):
        if _NEG.search(clause):
            continue
        if _ACTED.search(clause):
            return True
        if (m := _PASSIVE.search(clause)) and m.group(1).casefold() not in reported.replace("_", " "):
            return True
        if receipts == 0 and _DRAFTED.search(clause) and "draft" not in reported:
            return True
    return False


# --------------------------------------------------------------------------------------------------- attribution

_DIMENSIONS = {"native_id": "account", "native_customer_id": "account", "canonical_sku": "item", "native_item": "item",
               "backend_id": "system"}
_BASE_UNITS = {"unit", "units", "piece", "pieces", "pcs", "pc", "ea", "each", "st"}
_PACKAGES = {"box", "carton", "pack", "packet", "case", "pallet", "bag", "roll", "kit", "crate", "dozen", "pair", "set",
             "kg", "metre", "meter", "litre", "liter"}
_UNIT_AFTER = re.compile(r"\s*(?:base\s+)?([A-Za-z]+\d*)")


@dataclass(frozen=True)
class _Observed:
    number: Decimal
    identity: Mapping[str, frozenset[str]]
    field: str
    uom: str | None
    text: bool  # inside an explanation, whose wording already carries its own unit


def _observations(value: Any, inherited: Mapping[str, frozenset[str]], name: str = "", uom: str | None = None
                  ) -> Iterable[_Observed]:
    if isinstance(value, dict):
        own: dict[str, set[str]] = {}
        for key, dimension in _DIMENSIONS.items():
            if isinstance(value.get(key), str):
                own.setdefault(dimension, set()).add(value[key].casefold())
        identity = {**inherited, **{k: frozenset(v) for k, v in own.items()}}
        unit = value.get("native_uom")
        for key, v in value.items():
            yield from _observations(v, identity, str(key), unit.casefold() if isinstance(unit, str) else uom)
    elif isinstance(value, list):
        yield _Observed(Decimal(len(value)), {}, "entries", None, True)
        for v in value:
            yield from _observations(v, inherited, name, uom)
    elif isinstance(value, str):
        for tok in tokens(value):
            if (n := _number(tok)) is not None:
                yield _Observed(n, inherited, name, uom, True)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield _Observed(Decimal(str(value)), inherited, name, uom, False)


def _names(clause: str, observed: Iterable[_Observed]) -> dict[str, frozenset[str]]:
    """Identities of the results that the clause names, by dimension."""
    low = clause.casefold()
    out: dict[str, set[str]] = {}
    for o in observed:
        for dimension, values in o.identity.items():
            for v in values:
                if v and re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", low):
                    out.setdefault(dimension, set()).add(v)
    return {k: frozenset(v) for k, v in out.items()}


def _statements(text: str) -> Iterable[str]:
    """Clauses, split further at "and" when both halves state a number (each then has its own subject)."""
    for clause in _clauses(text):
        halves = re.split(r"\s+and\s+", clause)
        if len(halves) > 1 and all(any(_number(tok) is not None for tok in tokens(x)) and any(
                _number(tok) is None for tok in tokens(x)) for x in halves):
            yield from halves
        else:
            yield clause


def _unit_fits(word: str, number: str, o: _Observed, spoken: str) -> bool:
    low = word.casefold()
    stem = low[:-2] if low in ("boxes",) else low[:-1] if low.endswith("s") else low
    package = stem in _PACKAGES or bool(re.fullmatch(r"[A-Z]{2,3}\d*", word) and low not in _BASE_UNITS)
    if re.search(rf"(?<![\w.,-]){re.escape(number.casefold())}\s+{re.escape(low)}\b", spoken):
        return True  # a tool or the person used exactly this wording
    if o.text:
        return not package
    base_field = o.field.endswith("_base") or o.field == "quantity"
    if package:
        return not base_field and (o.uom is None or stem in (o.uom, o.uom.rstrip("0123456789")))
    if low in _BASE_UNITS and not base_field and o.field.endswith("_native") and o.uom is not None:
        return o.uom in _BASE_UNITS
    return True


def misattributed(text: str, results: Mapping[str, Any], evidence: Evidence) -> str | None:
    """A number of this turn's results that the text attaches to another account, item, system or unit. A bounded
    heuristic over explicitly named identifiers, not an interpretation of the sentence."""
    observed: list[_Observed] = []
    for call_id, payload in results.items():
        args = evidence.call_args.get(call_id)
        base = {d: frozenset({args[k].casefold()}) for k, d in _DIMENSIONS.items()
                if isinstance(args, dict) and isinstance(args.get(k), str)}
        observed += _observations(payload, base)
    said = TokenSet([evidence.user_text])
    spoken = " ".join([*(x for r in results.values() for x in leaves(r)), evidence.user_text]).casefold()
    for clause in _statements(text):
        named = _names(clause, observed)
        for m in _TOKEN.finditer(_CONSTANTS.sub(lambda c: " " * len(c.group(0)), clause)):
            token, number = m.group(0), _number(m.group(0))
            if number is None:
                continue
            same = [o for o in observed if o.number == number]
            if not same or said.has(token):
                continue
            own = [o for o in same if all(not named.get(d) or named[d] & ids for d, ids in o.identity.items())]
            if not own:
                return token
            unit = _UNIT_AFTER.match(clause, m.end())
            if unit and not any(_unit_fits(unit.group(1), token, o, spoken) for o in own):
                return f"{token} {unit.group(1)}"
    return None


# --------------------------------------------------------------------------------------------------- validation


def failure_code(problem: str | None) -> str | None:
    """Content-free branch code of a ``validate()`` problem (safe to persist: no model text)."""
    if problem is None:
        return None
    if problem == "SCHEMA_INVALID":
        return "SCHEMA_INVALID"
    if problem.startswith("GROUNDING_FAILED: unknown tool call"):
        return "UNKNOWN_TOOL_CALL"
    if problem.endswith(" does not resolve"):
        return "PATH_UNRESOLVED"
    for marker, code in (("is a whole record", "WHOLE_RECORD"), ("is not the value of", "VALUE_MISMATCH"),
                         ("does not state", "VALUE_MISMATCH"), ("contradicts", "POLARITY"),
                         ("action the assistant", "ACTION_OVERCLAIM"), ("in the summary", "SUMMARY_UNGROUNDED")):
        if marker in problem:
            return code
    return "NUMBER_UNGROUNDED"


def _same(stated: str, observed: Any) -> bool:
    text = " ".join(stated.split())
    want = " ".join(display(observed).split())
    if isinstance(observed, str):
        stated, full = text.strip("\"'"), want.strip("\"'")
        # a long text may be cut short by the model; the whole observed text is what is rendered
        return stated == full or (len(full) > 120 and len(stated) >= 40 and full.startswith(stated.rstrip(" .…")))
    if isinstance(observed, bool) or observed is None:
        return text.casefold() == want
    a, b = _number(text), _number(want)
    if isinstance(observed, list):
        scalars = [display(v) for v in observed if not isinstance(v, (dict, list))]
        if observed and len(scalars) == len(observed) and \
                [x.strip(" \"'") for x in text.strip("[]").split(",")] == scalars:
            return True
        return a is not None and a == b
    return text == want or (a is not None and a == b)


def check_fact(fact: _ParsedFact, results: Mapping[str, Any], evidence: Evidence) -> tuple[Fact | None, str | None]:
    sid, path = fact.source.tool_call_id, fact.source.field_path
    if sid not in results:
        return None, f"GROUNDING_FAILED: unknown tool call {sid}"
    found, records = locate(results[sid], path)
    if found is MISSING or not path.strip():
        return None, f"GROUNDING_FAILED: {path} does not resolve"
    observed = _unwrap(found)
    if isinstance(observed, dict):
        keys = ", ".join(list(observed)[:12])
        return None, f"GROUNDING_FAILED: {path} is a whole record; cite one of its fields ({keys})"
    label = record_label(records)
    if fact.value is not None:
        if not _same(fact.value, observed):
            return None, (f"GROUNDING_FAILED: the stated value is not the value of {path}, "
                          f"which is {display(observed)[:200]!r}")
        return Fact(claim=render(path, observed), source=fact.source, record=label), None
    claim = fact.claim or ""
    leaf = json.dumps(observed, ensure_ascii=False, default=str)
    allowed = TokenSet([leaf, display(observed), *_identity(records, evidence.call_args.get(sid))])
    for token in tokens(claim):
        if not allowed.has(token):
            return None, f"GROUNDING_FAILED: {token} not in {path}"
    if (wrong := misattributed(claim, {sid: results[sid]}, evidence)) is not None:
        return None, f"GROUNDING_FAILED: the claim contradicts {path}: {wrong} belongs to another record or unit"
    if path.endswith("credit_hold") or (isinstance(observed, str) and observed in ("HOLD", "CLEAR")):
        stated = hold_polarity(claim)
        if stated is not None and stated != observed:
            return None, f"GROUNDING_FAILED: the claim contradicts {path}, which is {observed}"
    if isinstance(observed, bool):
        negative = bool(re.search(r"\b(?:not|no|false|never|without)\b|n't", claim, re.IGNORECASE))
        if negative == observed:
            return None, f"GROUNDING_FAILED: the claim contradicts {path}, which is {display(observed)}"
    elif isinstance(observed, str) and len(observed) <= 80:
        if " ".join(observed.casefold().split()) not in " ".join(claim.casefold().split()):
            return None, f"GROUNDING_FAILED: the claim does not state {path}, which is {observed!r}"
    elif isinstance(observed, (int, float)):
        if not TokenSet([claim]).has(display(observed)):
            return None, f"GROUNDING_FAILED: the claim does not state {path}, which is {display(observed)}"
    return Fact(claim=claim, source=fact.source, record=label), None


def check_text(text: str, results: Mapping[str, Any], evidence: Evidence) -> str | None:
    """Summary and warnings: exact tokens, credit-hold polarity, no claim of actions that were not performed."""
    allowed = TokenSet([*(x for r in results.values() for x in leaves(r)),
                        *(x for a in evidence.call_args.values() for x in leaves(a)), evidence.user_text])
    known_ids = TokenSet([evidence.context_text])
    for token in tokens(text):
        if allowed.has(token) or (_number(token) is None and known_ids.has(token)):
            continue
        return (f"GROUNDING_FAILED: {token} in the summary or a warning was not reported by a tool in this turn"
                " (call the tool again, or leave it out)")
    if (wrong := misattributed(text, results, evidence)) is not None:
        return (f"GROUNDING_FAILED: {wrong} in the summary or a warning belongs to another account, item, system or"
                " unit in the tool results")
    if hold_contradiction(text, results):
        return ("GROUNDING_FAILED: a credit-hold statement in the summary contradicts or lacks an observation of this"
                " turn")
    if action_overclaim(text, evidence.receipts, results):
        return "GROUNDING_FAILED: the text claims an action the assistant did not and cannot perform"
    return None


@dataclass(frozen=True)
class Checked:
    answer: AssistantAnswer | None
    problem: str | None
    verified_facts: tuple[Fact, ...] = ()  # facts that passed on their own (kept by the fallback)


def check(final_json: str, results: Mapping[str, Any], evidence: Evidence | None = None) -> Checked:
    evidence = evidence or Evidence()
    try:
        parsed = _ParsedAnswer.model_validate(json.loads(final_json))
    except (ValidationError, ValueError, TypeError):
        return Checked(None, "SCHEMA_INVALID")
    facts: list[Fact] = []
    problem: str | None = None
    for raw in parsed.facts:
        ok, bad = check_fact(raw, results, evidence)
        if ok is not None:
            facts.append(ok)
        problem = problem or bad
    for text in (parsed.summary, *parsed.warnings):
        problem = problem or check_text(text, results, evidence)
    if problem is not None:
        return Checked(None, problem, tuple(facts))
    return Checked(AssistantAnswer(summary=parsed.summary, facts=facts, draft_operation_id=parsed.draft_operation_id,
                                   warnings=parsed.warnings, next_human_actions=parsed.next_human_actions),
                   None, tuple(facts))


def validate(final_json: str, results: Mapping[str, Any], evidence: Evidence | None = None
             ) -> tuple[AssistantAnswer | None, str | None]:
    """Returns ``(answer, None)`` or ``(None, problem)`` with problem ``SCHEMA_INVALID`` or ``GROUNDING_FAILED: …``."""
    c = check(final_json, results, evidence)
    return c.answer, c.problem
