"""Fixtures: offline harnesses always; live harnesses (marker ``live``) need the test-profile simulators."""

from __future__ import annotations

import pytest

from .harness import HaldenFakeHarness, SkarvikFakeHarness

OFFLINE = ["halden_fake", "skarvik_fake"]
LIVE = ["halden_live", "skarvik_live"]


def pytest_generate_tests(metafunc):
    if "backend" in metafunc.fixturenames:
        params = [pytest.param(n, id=n) for n in OFFLINE]
        params += [pytest.param(n, id=n, marks=pytest.mark.live) for n in LIVE]
        metafunc.parametrize("backend", params, indirect=True)


@pytest.fixture
def backend(request, tmp_path):
    # A generator fixture must yield on every path (the original ``return`` on the offline paths made pytest
    # raise "backend did not yield a value" for every offline test).
    name = request.param
    if name == "halden_fake":
        harness = HaldenFakeHarness()
    elif name == "skarvik_fake":
        harness = SkarvikFakeHarness(tmp_path)
    else:
        from .live import make_live

        harness = make_live(name)
    try:
        yield harness
    finally:
        harness.close()
