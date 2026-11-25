"""Run the simulator: pickup loop, ack/extract ticker, optional control plane."""

from __future__ import annotations

import signal
import sys
import threading
import time

from skarvik.control import serve
from skarvik.simulator import Config, Simulator


def main() -> int:
    config = Config.from_env()
    sim = Simulator(config)
    notes = sim.startup_recovery()
    for note in notes:
        print(f"recovery: {note}", flush=True)
    sim.generate_extracts(force=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if config.sim_control:
        server = serve(sim, config.control_port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"control plane on :{config.control_port}", flush=True)
    heartbeat = config.data_dir / "heartbeat"
    last_pickup = 0.0
    print("SKARVIK OES 4 simulator running", flush=True)
    while not stop.is_set():
        now = time.monotonic()
        if now - last_pickup >= config.pickup_interval:
            for outcome in sim.pickup_once():
                print(f"processed: {outcome}", flush=True)
            last_pickup = now
        sim.tick()
        heartbeat.write_text(str(time.time()))
        stop.wait(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
