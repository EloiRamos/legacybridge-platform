"""Kill harness: ``docker kill --signal=KILL`` of the application container at a reached pause point,
then ``docker compose --profile test up -d app`` (see ``Stack.restart_app``)."""

from __future__ import annotations

import os
import subprocess
import time

APP_CONTAINER = f"{os.environ.get('LBF_NS') or 'legacybridge-platform'}-app-1"


def kill_app() -> float:
    """SIGKILL the application container; returns the wall-clock kill timestamp (evidence)."""
    from resilience.harness.stack import prove_ownership

    prove_ownership()
    at = time.time()
    subprocess.run(["docker", "kill", "--signal=KILL", APP_CONTAINER], check=True,  # noqa: S603, S607
                   capture_output=True)
    return at
