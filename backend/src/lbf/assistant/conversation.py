"""The conversation of one assistant session: server-held, in memory, bounded.

Never supplied by the client and never written to the database, logs or audit (no stored transcripts). It is
keyed by the caller with user and server session, so it ends with that session (sign-out, expiry, re-authentication)
and with the process. Earlier answers are context only: grounding accepts the current turn's tool results alone.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from datetime import datetime, timedelta

from lbf.assistant.answer import AssistantAnswer
from lbf.assistant.provider import ProviderMessage

MAX_TURNS = 6
MAX_BYTES = 8 * 1024
MAX_SESSIONS = 200
TTL = timedelta(minutes=30)  # the idle timeout of the session it belongs to


def remembered(answer: AssistantAnswer) -> str:
    """The answer as later turns see it: no tool-call ids or field paths (they do not exist in a later turn)."""
    return json.dumps({"summary": answer.summary,
                       "facts": [f"{f.record}: {f.claim}" if f.record else f.claim for f in answer.facts],
                       "warnings": answer.warnings,
                       "draft_operation_id": str(answer.draft_operation_id) if answer.draft_operation_id else None},
                      ensure_ascii=False)


class ConversationStore:
    def __init__(self, clock: Callable[[], datetime], max_sessions: int = MAX_SESSIONS) -> None:
        self._clock = clock
        self._max = max_sessions
        self._lock = threading.Lock()
        self._items: OrderedDict[Hashable, tuple[datetime, list[tuple[str, str]]]] = OrderedDict()

    def _prune(self, now: datetime) -> None:
        for key in [k for k, (seen, _) in self._items.items() if now - seen > TTL]:
            del self._items[key]
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def history(self, key: Hashable) -> list[ProviderMessage]:
        with self._lock:
            self._prune(self._clock())
            turns = list(self._items[key][1]) if key in self._items else []
        return [m for q, a in turns for m in (ProviderMessage(role="user", text=q),
                                              ProviderMessage(role="assistant", text=a))]

    def add(self, key: Hashable, question: str, answer: str) -> None:
        now = self._clock()
        with self._lock:
            turns = [*(self._items[key][1] if key in self._items else []), (question, answer)][-MAX_TURNS:]
            while turns and sum(len(q.encode()) + len(a.encode()) for q, a in turns) > MAX_BYTES:
                turns.pop(0)
            self._items[key] = (now, turns)
            self._items.move_to_end(key)
            self._prune(now)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
