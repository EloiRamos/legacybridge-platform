"""Builds the assistant from the environment (fail-closed; default ``disabled``)."""

from __future__ import annotations

from typing import Any

from lbf.api.services import AppServices
from lbf.assistant.budget import BudgetService
from lbf.assistant.config import AssistantConfig
from lbf.assistant.providers.disabled import DisabledProvider
from lbf.assistant.service import AssistantService
from lbf.assistant.telemetry import Telemetry


def build_assistant(svc: AppServices, config: AssistantConfig | None = None, provider: Any = None) -> AssistantService:
    config = config or AssistantConfig.from_env()
    if provider is None:
        if config.provider == "openai":
            from lbf.assistant.providers.openai_provider import OpenAIProvider

            provider = OpenAIProvider(config, budget=BudgetService(svc.engine, config, svc.clock),
                                      telemetry=Telemetry(svc.engine, svc.clock), clock=svc.clock)
            if not provider.ready:  # fail closed: misconfigured live inference ⇒ deterministic router
                svc.extras["assistant_status"] = "misconfigured"
                provider = DisabledProvider()
        elif config.provider == "scripted":
            from lbf.assistant.providers.scripted import ScriptedProvider

            provider = ScriptedProvider([])
        else:
            provider = DisabledProvider()
    svc.extras.setdefault("assistant_status", config.provider if provider.name != "disabled" else "disabled")
    assistant = AssistantService(svc, provider, config)
    try:
        assistant.record_config()
    except Exception:  # noqa: S110 - a configuration audit failure must not prevent the API from starting
        pass
    return assistant
