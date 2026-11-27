"""Synthetic visitors for the skill run, through the Flagsmith Python SDK. The harness starts it as a side process.

Each visitor is a new identifier. It asks Flagsmith for the experiment flag with ``get_experiment_flag`` (local
evaluation with the environment's server-side key, so Flagsmith assigns the arm and records the exposure), then
reports a ``purchase`` with a probability this script chooses for the arm it was served (10% on control, 15% on the
variation by default: a real effect) and a ``checkout_error`` at the same rate in both arms (a guardrail with no
real difference). Flagsmith computes every number from those events; nothing here reads results.

The server-side key comes from ``SKILL_RUN_SERVER_KEY`` in the environment (the harness sets it), never argv.

    python -m examples.skill_run.traffic --flag ulysses_skill_cta --control-value classic --per-minute 100
"""

from __future__ import annotations

import argparse
import os
import random
import secrets
import signal
import sys
import time


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-url", default="http://localhost:8000")
    ap.add_argument("--flag", required=True)
    ap.add_argument("--control-value", default="classic")
    ap.add_argument("--per-minute", type=float, default=100)
    ap.add_argument("--minutes", type=float, default=90)
    ap.add_argument("--control-rate", type=float, default=0.10)
    ap.add_argument("--variant-rate", type=float, default=0.15)
    ap.add_argument("--error-rate", type=float, default=0.02, help="checkout_error, same in both arms")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--stop-file", help="stop cleanly (flushing buffered events) once this file exists")
    a = ap.parse_args(argv)
    key = os.environ.get("SKILL_RUN_SERVER_KEY")
    if not key:
        print("no SKILL_RUN_SERVER_KEY in the environment", flush=True)
        return 2

    from flagsmith import Flagsmith
    from flagsmith.analytics import EventProcessorConfig

    fs = Flagsmith(environment_key=key, api_url=a.api_url + "/api/v1/", enable_local_evaluation=True,
                   environment_refresh_interval_seconds=10, enable_events=True,
                   event_processor_config=EventProcessorConfig(events_api_url=a.api_url + "/api/",
                                                               flush_interval_seconds=2.0))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    rng = random.Random(a.seed)
    run = secrets.token_hex(3)
    gap = 60.0 / a.per_minute
    end = time.time() + a.minutes * 60
    counts: dict[str, list[int]] = {}
    i = 0
    print(f"synthetic visitors {run}: {a.per_minute:g}/min; purchase {a.control_rate:.0%} on control, "
          f"{a.variant_rate:.0%} on the variation; checkout_error {a.error_rate:.0%} in both", flush=True)
    try:
        while time.time() < end and not stop["now"] and not (a.stop_file and os.path.exists(a.stop_file)):
            t0 = time.time()
            visitor = f"skillrun-{run}-{i:06d}"
            i += 1
            try:
                flag = fs.get_experiment_flag(a.flag, identifier=visitor)
                value = str(flag.value) if flag.enabled and flag.value is not None else a.control_value
                arm = "variation" if value != a.control_value else "control"
                c = counts.setdefault(arm, [0, 0, 0])
                c[0] += 1
                if rng.random() < (a.variant_rate if arm == "variation" else a.control_rate):
                    fs.track_event("purchase", identifier=visitor)
                    c[1] += 1
                if rng.random() < a.error_rate:
                    fs.track_event("checkout_error", identifier=visitor)
                    c[2] += 1
            except Exception as exc:  # noqa: BLE001 - keep the traffic going; report and slow down
                print(f"visitor problem: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(5)
            if i % 500 == 0:
                print(f"{i} visitors; seen/purchase/checkout_error {counts}", flush=True)
            time.sleep(max(0.0, gap - (time.time() - t0)))
    finally:
        try:
            fs.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"done: {i} visitors; seen/purchase/checkout_error {counts}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
