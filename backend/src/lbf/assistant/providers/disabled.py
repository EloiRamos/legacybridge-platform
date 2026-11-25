"""``DisabledProvider`` — the default: every turn goes to the deterministic guided-intent router."""

from __future__ import annotations

from lbf.assistant.provider import ErrorCategory, ProviderRequest, ProviderTurn


class DisabledProvider:
    name = "disabled"
    model = ""

    def run(self, request: ProviderRequest, max_attempts: int | None = None) -> ProviderTurn:
        return ProviderTurn(kind="ERROR", error_category=ErrorCategory.CONFIG_REJECTED)
