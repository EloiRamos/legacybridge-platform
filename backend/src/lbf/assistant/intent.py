"""Intent gate for the two drafting tools.

The model proposes structured arguments; it is not trusted to have understood the request, and its choice of a tool is
not a command. A draft is written directly only when the **whole message** is a drafting command of a small closed
grammar (:data:`_CREATE`, :data:`_REVISE`) whose every word is accounted for, and every argument is the person's own.
A message that contains anything else (a quotation, an explanation, a condition, a time, a second order, another
language) is *unconfirmed*: the person sees the exact proposal and decides. That is the safe direction, and it is not
a general understanding of natural language. Explicitly invalid quantities (negative, zero, fractional) are refused
outright and never turned into a proposal.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from lbf.domain.models import DraftContent, DraftContentLine

Verdict = Literal["ESTABLISHED", "UNCONFIRMED", "INVALID"]


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    problems: tuple[str, ...] = ()


_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_UNIT_WORDS = {"unit", "units", "pcs", "pc", "piece", "pieces", "ea", "each"}
# Neighbours a plain quantity may have. Anything else leaves the quantity unconfirmed.
_BEFORE = {
    "", ":", ",", ".", ";", "(", "=", "x", "for", "of", "with", "to", "and", "draft", "order", "prepare", "create",
    "use", "quantity", "qty", "exactly", "only", "units", "need", "needs", "want", "wants", "please", "item", "is",
    "be", "it", "set", "revise", "revision", "then", "also"}
_AFTER = {"", ":", ",", ".", ";", ")", "!", "x", "of", "and", "for", "to", "on", "in", "please", "with", "only", "at",
          "from", "as", "by", "then", "also", *_UNIT_WORDS}
# Words that change what a number means, or make the request conditional, relative, packaged or negated.
_UNCERTAIN = {
    "per", "box", "boxes", "carton", "cartons", "pack", "packs", "packet", "case", "cases", "pallet", "pallets",
    "dozen", "dozens", "pair", "pairs", "sets", "kit", "kits", "bag", "bags", "roll", "rolls", "kg", "g", "lb",
    "lbs", "metre", "metres", "meter", "meters", "litre", "litres", "liter", "liters", "couple", "few", "several",
    "some", "about", "approx", "approximately", "around", "roughly", "circa", "least", "most", "up", "between",
    "or", "maybe", "perhaps", "either", "not", "don't", "never", "instead", "rather", "actually", "sorry",
    "correction", "double", "triple", "twice", "plus", "add", "adding", "more", "fewer", "less", "increase",
    "decrease", "extra", "additional", "another", "remaining", "rest", "same", "if", "unless", "whether", "would",
    "could", "might", "hypothetically", "return", "returns", "returned", "refund", "credit", "remove", "cancel",
    "subtract", "deduct", "reverse", "off", "back", "hundred", "thousand", "million", "former", "previous",
    "original", "without", "except", "ignore"}
_INVALID_WORDS = {"minus", "negative", "zero", "half", "quarter", "third", "nought"}

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_SKU_LIKE = re.compile(r"\b[a-z]{2,5}-\d{2,6}(?:-[a-z0-9]+)?\b")
_SAFE_PHRASES = re.compile(
    r"\b(?:do not|don't|never|without)\s+(?:submit|submitting|approve|approving|send|sending|transmit|transmitting)\b"
    r"(?:\s+(?:it|this|anything|the order|the draft))?"
    r"|\breturn (?:me )?(?:a|the) (?:direct )?(?:draft )?(?:link|id|identifier|reference)\b"
    r"(?: if you (?:create|prepare) it)?")
_ONE_DRAFT = re.compile(r"\b(?:one|1|a|single)\s+(?:single\s+)?(?:new\s+)?(?=draft\b|order draft\b)(?!drafts)")
_PRODUCT = re.compile(r"\b(?:halden(?: erp)? 3\.1|skarvik(?: oes)? 4)\b")
_VERSION = re.compile(r"\b(?:expected |current )?version\s+(\d+)\b|\bv(\d+)\b")

# --------------------------------------------------------------------------- the closed command grammar
# Applied to a normalized message in which values are replaced by markers: §c account, §i<n> item of line n, §o draft
# id, §r version, §v customer PO, §d date, §p product name with version, and number words by digits.
_SYS = r"(?:halden|skarvik|§p)"
_POLITE = r"(?:please )?"
_UNIT = r"(?: (?:units?|pcs|pc|pieces?|ea|each))?"
_ACCOUNT = rf"(?:the )?(?:{_SYS} )?(?:(?:account|customer)(?: number)? )?§c"
_WHERE = rf"(?:(?:for|in|to) {_ACCOUNT}(?: (?:in|at) {_SYS})?|(?:in|for) {_SYS})"
_LINE = (rf"(?:(?:exactly |only )?(?:quantity |qty )?\d+{_UNIT} (?:(?:x|of) )?(?:item )?§i\d+"
         rf"|(?:item )?§i\d+ (?:[:,=] )?(?:x )?(?:quantity |qty )?\d+{_UNIT})")
_LINES = rf"(?:with |of |for )?{_LINE}"
_ATTR = (r"(?:(?:with|and) )?(?:(?:customer )?(?:po|purchase order)(?: number)? (?:: )?§v"
         r"|(?:requested|delivery) date (?:: )?§d)")
_SEP = r"(?: [,:;]| and| , and)?"
_OBJECT = rf"(?:(?:a|an|one|1|the) )?(?:(?:single|new) )*(?:{_SYS} )?(?:order draft|draft order|order|draft)(?: only)?"
_CREATE = re.compile(
    rf"(?:(?:for )?{_ACCOUNT} [,:] )?{_POLITE}"
    rf"(?:(?:create|prepare|draft) {_OBJECT}|draft(?= (?:exactly |only )?(?:\d|§i)))"
    rf"(?:{_SEP} (?:{_WHERE}|{_LINES}|{_ATTR}))+"
    r"(?: ,)?(?: (?:and|then)(?: just)? submit it(?: for approval)?)?(?: please)?(?: ,)?")
_SET_LINE = rf"(?:{_LINE}|(?:item )?§i\d+ to \d+{_UNIT})"
_SET_LINES = rf"{_SET_LINE}(?: (?:,|;|and|, and) {_SET_LINE})*"
_DRAFT_REF = r"(?:the )?(?:existing )?draft §o(?: ,)?(?:(?: \(| at| using)? §r(?: \))?)?(?: ,)?"
_REVISE = re.compile(
    rf"{_POLITE}(?:(?:revise|change|update) {_DRAFT_REF}(?: :)? (?:to |so that |with |and set |set )?{_SET_LINES}"
    rf"|apply a revision to {_DRAFT_REF} (?:and )?set {_SET_LINES}"
    rf"|(?:make it|set it to|change it to|set) {_SET_LINES})(?: please)?(?: ,)?")
_TAILS = re.compile(r"(?:(?:stop at draft|keep it (?:a )?draft|draft only|report what happened|thanks|thank you"
                    r"|please)(?: ,)?|,?)")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"[‒–—−﹣]", "-", t)
    t = t.replace("§", " ").replace("’", "'").replace("×", "x")
    return " ".join(t.split())


def _bounded(value: str) -> re.Pattern[str]:
    # "BW-1001x2" and "2xBW-1001" keep their item; "BW-10010" is not BW-1001
    return re.compile(rf"(?<![a-z0-9-]){re.escape(value)}(?=x\s?\d|[^a-z0-9-]|-?$|-\s)")


def _parse(words: list[str]) -> list[str]:
    """Number words to digits ('twenty', 'five' → '25')."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in _TENS:
            value = _TENS[w]
            j = i + 1
            if j < len(words) and words[j] == "-" and j + 1 < len(words) and words[j + 1] in _UNITS:
                value += _UNITS[words[j + 1]]
                j += 2
            elif j < len(words) and words[j] in _UNITS and _UNITS[words[j]] < 10:
                value += _UNITS[words[j]]
                j += 1
            out.append(str(value))
            i = j
            continue
        out.append(str(_UNITS[w]) if w in _UNITS else w)
        i += 1
    return out


def is_command(words: list[str], tool: str) -> bool:
    """Whether the whole marked-up message is one drafting command (and nothing else but a few harmless closings)."""
    sentences = [s.strip() for s in re.split(r"[.!]", " ".join(words))]
    command = _CREATE if tool == "prepare_draft" else _REVISE
    return bool(command.fullmatch(sentences[0])) and all(_TAILS.fullmatch(s) for s in sentences[1:])


def assess(user_text: str, tool: str, args: dict[str, Any],
           known_drafts: Mapping[str, int] | Collection[str] = frozenset()) -> Assessment:
    """Whether the person's own text establishes these drafting arguments. ``known_drafts``: the drafts this session's
    receipts named, with their last verified version when known."""
    t = normalize(user_text)
    problems: list[str] = []

    # ------------------------------------------------------------------ identifiers must be the person's own
    work = t
    if tool == "revise_draft":
        op = str(args["operation_id"])
        named_ops = set(_UUID.findall(t))
        stated = {int(a or b) for a, b in _VERSION.findall(_PRODUCT.sub(" ", t))}
        if named_ops - {op}:
            problems.append("the request names another draft as well")
        if not named_ops and (op not in known_drafts or len(known_drafts) != 1):
            problems.append("the draft to revise was not named in this request")
        if stated - {args["expected_version"]}:
            problems.append(f"the request names another version than {args['expected_version']}")
        elif not named_ops and isinstance(known_drafts, Mapping) and not stated and \
                known_drafts.get(op, args["expected_version"]) != args["expected_version"]:
            problems.append("the draft's version is not the one this session last stored")
    else:
        customer = normalize(str(args["native_customer_id"]))
        if not _bounded(customer).search(t):
            problems.append(f"the account number {args['native_customer_id']} was not given in this request")
        work = _bounded(customer).sub(" §c ", work)
        backend, other = args["backend_id"], "skarvik" if args["backend_id"] == "halden" else "halden"
        named, other_named = backend in t, other in t
        implied = customer.startswith("knd-") if backend == "skarvik" else bool(re.fullmatch(r"\d{5}", customer))
        if other_named and not named:
            problems.append(f"the request names {other.upper()}, not {backend.upper()}")
        elif (other_named and named) or not (named or implied):
            problems.append("the legacy system is not clear from the request")
        for key in ("customer_po", "requested_date", "delivery_note"):
            value = args.get(key)
            if value:
                v = normalize(str(value))
                if v not in t:
                    problems.append(f"{key} was not given in this request")
                if key == "delivery_note":
                    problems.append("a delivery note is free text and is confirmed before it is stored")
                elif key == "customer_po":
                    if re.fullmatch(r"[\w./-]+", v) and any(c.isdigit() for c in v):
                        work = _bounded(v).sub(" §v ", work)
                    else:
                        problems.append("the customer PO is not a plain reference")
    work = re.sub(r"(?<![\w-])(\d{1,4})x(?=[a-z])", r"\1 x ", work)
    work = _UUID.sub(" §o ", work)
    work = _VERSION.sub(" §r ", _PRODUCT.sub(" §p ", work))
    dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", work))
    if dates - {args.get("requested_date")}:
        problems.append("the request contains a date that is not part of the proposed draft")
    work = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " §d ", work)

    items: list[str] = []
    for i, line in enumerate(args["lines"]):
        ids = [normalize(str(line[k])) for k in ("canonical_sku", "native_item") if line.get(k)]
        hit = False
        for ident in ids:
            if _bounded(ident).search(work):
                hit = True
                work = _bounded(ident).sub(f" §i{i} ", work)
        if not hit:
            name = line.get("canonical_sku") or line.get("native_item")
            problems.append(f"the item {name} was not named in this request")
        items.append("|".join(ids))
    if _SKU_LIKE.search(work):
        problems.append("the request names another item as well")
        work = _SKU_LIKE.sub(" §x ", work)

    # ------------------------------------------------------------------ quantities
    body = _SAFE_PHRASES.sub(" ", work)
    body = _ONE_DRAFT.sub(" ", body)
    if re.search(r"(?<!\w)-\d", body):
        return Assessment("INVALID", ("a quantity is negative; a draft needs a whole number from 1 to 999",))
    if re.search(r"(?<!\d)\d+[.,]\d+|\d+\s*/\s*\d+|\d+\s*%", body):
        return Assessment("INVALID", ("a quantity is not a whole number; a draft needs a whole number from 1 to 999",))
    spaced = re.sub(r"(\d)x|x(\d)", _split_x, body)
    words = _parse(re.findall(r"§[a-z]\d*|[^\W\d_]+(?:'[^\W\d_]+)*|\d+|[^\w\s]", spaced))
    bad = [w for w in words if w in _INVALID_WORDS or (w.isdigit() and int(w) == 0)]
    if bad:
        return Assessment("INVALID", (f"the request contains '{bad[0]}'; a draft needs a whole number from 1 to 999",))
    if any(w.isdigit() and int(w) > 999 for w in words):
        return Assessment("INVALID", ("a quantity is above 999; a draft needs a whole number from 1 to 999",))
    uncertain = [w for w in words if w in _UNCERTAIN]
    if uncertain:
        problems.append(f"'{uncertain[0]}' leaves the requested quantity or intent open")
    if "?" in words:
        problems.append("the request is phrased as a question")
    if not is_command(words, tool):
        problems.append("the message is not, as a whole, a plain drafting command")
    seq: list[tuple[str, int]] = []  # ("N", value) | ("I", line index)
    for i, w in enumerate(words):
        if w.startswith("§i"):
            seq.append(("I", int(w[2:])))
        elif w.isdigit():
            before, after = words[i - 1] if i else "", words[i + 1] if i + 1 < len(words) else ""
            plain = (before in _BEFORE or before.startswith("§")) and (after in _AFTER or after.startswith("§"))
            if not plain:
                problems.append(f"'{' '.join(x for x in (before, w, after) if x)}' is not a plain quantity")
            seq.append(("N", int(w)))
    problems += _bind(seq, args["lines"])
    return Assessment("UNCONFIRMED", tuple(dict.fromkeys(problems))) if problems else Assessment("ESTABLISHED")


def _split_x(m: re.Match[str]) -> str:
    return f"{m.group(1)} x " if m.group(1) else f" x {m.group(2)}"


def _bind(seq: list[tuple[str, int]], lines: list[dict[str, Any]]) -> list[str]:
    numbers = [v for k, v in seq if k == "N"]
    if not numbers:
        return ["no quantity was given in this request"]
    if len(lines) == 1:
        if len(numbers) != 1:
            return ["the request contains more than one number that could be the quantity"]
        return [] if numbers[0] == lines[0]["quantity"] else \
            [f"the request says {numbers[0]}, not {lines[0]['quantity']}"]
    kinds = "".join(k for k, _ in seq)
    if len(numbers) != len(lines) or kinds not in ("NI" * len(lines), "IN" * len(lines)):
        return ["the quantities cannot be matched to the items one by one"]
    values = [v for _, v in seq]
    pairs = [(values[i + 1], values[i]) if kinds.startswith("N") else (values[i], values[i + 1])
             for i in range(0, len(values), 2)]
    if sorted(i for i, _ in pairs) != list(range(len(lines))):
        return ["the quantities cannot be matched to the items one by one"]
    wrong = [i for i, q in pairs if lines[i]["quantity"] != q]
    return [f"the quantity of {lines[i].get('canonical_sku') or lines[i].get('native_item')} differs from the request"
            for i in wrong]


@dataclass(frozen=True)
class RevisionBase:
    """The stored version a revision would replace."""

    version: int
    content: DraftContent

    def removed(self, args: dict[str, Any]) -> list[DraftContentLine]:
        """Stored lines the revision does not mention (a revision replaces all lines)."""
        kept = {str(ln[k]) for ln in args["lines"] for k in ("canonical_sku", "native_item") if ln.get(k)}
        return [ln for ln in self.content.lines if ln.native_item not in kept and ln.canonical_sku not in kept]


_HEADER = (("customer_po", "customer PO"), ("requested_date", "requested date"), ("delivery_note", "delivery note"))


def describe(tool: str, args: dict[str, Any], base: RevisionBase | None = None) -> str:
    """The exact and complete structured mutation, in words, for the person to confirm."""
    lines = ", ".join(f"{ln['quantity']} base unit(s) of {ln.get('canonical_sku') or ln.get('native_item')}"
                      for ln in args["lines"])
    if tool == "revise_draft":
        text = f"Replace the lines of draft {args['operation_id']} (version {args['expected_version']}) with: {lines}."
        if base is None:
            return text
        before = ", ".join(f"{ln.quantity_base} base unit(s) of {ln.canonical_sku or ln.native_item}"
                           for ln in base.content.lines)
        gone = ", ".join(str(ln.canonical_sku or ln.native_item) for ln in base.removed(args))
        kept = ", ".join(f"{label} {getattr(base.content, key)}" for key, label in _HEADER
                         if getattr(base.content, key))
        return (f"{text} Lines now: {before}." + (f" REMOVED: {gone}." if gone else "")
                + f" Unchanged: {base.content.backend_id.upper()} account {base.content.native_customer_id}"
                + (f", {kept}." if kept else ", no customer PO, requested date or delivery note."))
    extra = "".join(f", {label} {args[key]}" for key, label in _HEADER if args.get(key))
    return f"Create a DRAFT in {args['backend_id'].upper()} for account {args['native_customer_id']}: {lines}{extra}."
