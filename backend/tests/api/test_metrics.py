"""OPS-02: the Prometheus scrape exposes operations by state, transmissions by kind/observation, the age of the oldest
UNKNOWN operation, the engine leader flag and backend health."""

from __future__ import annotations

import re

import pytest
from api_support import Api
from workflow_support import WF

pytestmark = pytest.mark.db


@pytest.fixture
def api(app_engine, tmp_path):
    return Api(WF(app_engine, tmp_path))


def test_ops_02_metrics_present(api):
    confirmed = api.ready()
    api.as_("marta").post(f"/api/v1/operations/{confirmed}/approve", json=api.version_ref(confirmed))
    api.wf.step()
    unknown = api.ready(sku="BW-1004", qty=1)
    api.as_("marta").post(f"/api/v1/operations/{unknown}/approve", json=api.version_ref(unknown))
    api.wf.halden.next_e8_modes.append("lost_before_backend")
    api.wf.step()
    api.wf.clock.advance(90)
    m = api.anon().get("/metrics").text
    assert re.search(r'^lbf_operations\{state="CONFIRMED"\} 1\.0$', m, re.M), m[:2000]
    assert re.search(r'^lbf_operations\{state="UNKNOWN"\} 1\.0$', m, re.M)
    assert re.search(r'^lbf_transmissions_total\{[^}]*kind="SUBMIT"[^}]*\} [1-9]', m, re.M)
    age = re.search(r"^lbf_unknown_age_max_seconds ([0-9.e+]+)$", m, re.M)
    assert age and float(age.group(1)) >= 90
    assert re.search(r"^lbf_engine_leader ", m, re.M)
    assert re.search(r'^lbf_backend_up\{[^}]*backend="halden"[^}]*\} ', m, re.M)
    assert re.search(r'^lbf_backend_up\{[^}]*backend="skarvik"[^}]*\} ', m, re.M)
