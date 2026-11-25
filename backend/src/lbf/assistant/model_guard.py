"""Fail-closed check of the model a provider reports (model rule, R-1 … R-6).

A response is used only if it reports the authorized identifier or its dated form. Anything else closes the guard, and a
closed guard never opens again. ``openai_provider.PROCESS_GUARD`` covers the process: after an unexpected identifier no
further request is sent until it is restarted. A campaign has its own guard as well, which also pins the first accepted
identifier and is told durably about the abort. The dated form is a validation grammar; it does not assert that such
identifiers exist and it is never a permitted configuration.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import date

from lbf.assistant.config import AUTHORIZED_OPENAI_MODEL

_DATED = re.compile(re.escape(AUTHORIZED_OPENAI_MODEL) + r"-([0-9]{4})-([0-9]{2})-([0-9]{2})")


def accepted(reported: object) -> bool:
    if not isinstance(reported, str):
        return False
    if reported == AUTHORIZED_OPENAI_MODEL:
        return True
    dated = _DATED.fullmatch(reported)
    if dated is None:
        return False
    try:
        date(*(int(part) for part in dated.groups()))
    except ValueError:
        return False
    return True


def recordable(reported: object) -> str | None:
    """The reported value as a text column can hold it: strings verbatim, other JSON values as JSON. A string the
    database cannot store (a NUL or an unpaired surrogate) is kept as its JSON escape."""
    if reported is None:
        return None
    if isinstance(reported, str) and "\x00" not in reported and not any("\ud800" <= c <= "\udfff" for c in reported):
        return reported
    return json.dumps(reported, default=repr)


class ModelGuard:
    def __init__(self, *, pin_version: bool = False, observed: str | None = None,
                 on_abort: Callable[[object], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._pin = pin_version
        self._on_abort = on_abort
        self.observed = observed  # the first accepted identifier
        self.closed = False
        self.unexpected: object = None

    def observe(self, reported: object) -> bool:
        """Whether this response may be used. Every received response is observed before anything reads it."""
        with self._lock:
            if self.closed:
                return False
            if accepted(reported) and not (self._pin and self.observed is not None and reported != self.observed):
                self.observed = self.observed or str(reported)
                return True
        self.close(reported)
        return False

    def close(self, reported: object) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self.unexpected = reported
        if self._on_abort is not None:
            self._on_abort(reported)
