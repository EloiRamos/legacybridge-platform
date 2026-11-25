"""Assistant test fixtures (helpers in ``assistant_helpers``)."""

from __future__ import annotations

import pytest
from api_support import Api
from assistant_helpers import Harness, MockOpenAI
from workflow_support import WF


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


@pytest.fixture
def mock_openai():
    return MockOpenAI()


@pytest.fixture
def h(api):
    return Harness(api)


@pytest.fixture(autouse=True)
def _new_process(monkeypatch):
    """Each test stands for a new process: the model latch closes until the process is restarted."""
    from lbf.assistant.model_guard import ModelGuard
    from lbf.assistant.providers import openai_provider

    monkeypatch.setattr(openai_provider, "PROCESS_GUARD", ModelGuard())
