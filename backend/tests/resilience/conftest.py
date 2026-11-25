"""In-process resilience variants (R07 two processes, R08 engine claim race, R16, R17, R18a).

Each test asserts its own expectation and, when ``LBF_RES_OBS_DIR`` is set (by ``resilience/run.py``), also writes
its observations to ``<dir>/<scenario>__<variant>.json`` so the runner can compare them with
``resilience/expected/R<nn>.yaml`` and record them as evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from api_support import Api
from resilience_support import record_observation
from workflow_support import WF


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


@pytest.fixture
def record() -> Callable[[str, str, dict[str, Any]], None]:
    return record_observation
