"""Simulated visitors for the demo shop.

This is the traffic source for the demo experiments, and it is synthetic: each
visitor is a new identifier, opens the checkout page (Flagsmith assigns the
variations of both demo flags and records the exposures), and then buys with a
probability this script chooses for what they saw. The default behaviour model
gives the bold button a real effect (purchases 10% after classic, 17% after
bold) and the same refund-request rate in both arms. The headline flag is an
A/A flag: both of its variations show the same text and nothing in this script
depends on it, so any difference Flagsmith reports between its arms is noise.

Nothing here reads Flagsmith's results; the shop reports events and Flagsmith
computes the numbers.

    uv run python examples/shop/visitors.py --per-minute 90 --minutes 40
"""

from __future__ import annotations

import argparse
import random
import re
import secrets
import sys
import time

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shop", default="http://127.0.0.1:8210")
    ap.add_argument("--per-minute", type=float, default=90)
    ap.add_argument("--minutes", type=float, default=45)
    ap.add_argument("--classic", type=float, default=0.10, help="purchase probability after seeing classic")
    ap.add_argument("--bold", type=float, default=0.17, help="purchase probability after seeing bold")
    ap.add_argument("--refund-classic", type=float, default=0.30, help="refund probability after a classic purchase")
    ap.add_argument("--refund-bold", type=float, default=0.30, help="refund probability after a bold purchase")
    ap.add_argument("--max-failures", type=int, default=30, help="consecutive failures before giving up")
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    run = secrets.token_hex(3)
    gap = 60.0 / a.per_minute
    end = time.time() + a.minutes * 60
    counts = {"classic": [0, 0, 0], "bold": [0, 0, 0]}
    i = failures = 0
    with httpx.Client(base_url=a.shop, timeout=20) as http:
        print(f"simulated visitors run {run}: {a.per_minute}/min for {a.minutes} min (synthetic behaviour: "
              f"purchase classic {a.classic:.0%}, bold {a.bold:.0%}; refunds {a.refund_classic:.0%}/"
              f"{a.refund_bold:.0%} of purchases)", flush=True)
        while time.time() < end:
            t0 = time.time()
            visitor = f"visitor-{run}-{i:06d}"
            i += 1
            try:
                r = http.get("/", params={"visitor": visitor})
                r.raise_for_status()
                m = re.search(r'data-cta="([^"]+)"', r.text)
                if not m:
                    raise ValueError("the page has no call to action; is the shop serving the experiment?")
                seen = m.group(1)
                counts.setdefault(seen, [0, 0, 0])[0] += 1
                if rng.random() < (a.bold if seen == "bold" else a.classic):
                    http.post("/buy", data={"visitor": visitor}).raise_for_status()
                    counts[seen][1] += 1
                    if rng.random() < (a.refund_bold if seen == "bold" else a.refund_classic):
                        http.post("/refund", data={"visitor": visitor}).raise_for_status()
                        counts[seen][2] += 1
                failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                print(f"shop problem ({failures}/{a.max_failures}): {exc}", flush=True)
                if failures >= a.max_failures:
                    print("giving up: the shop is not serving the experiment", flush=True)
                    sys.exit(2)
                time.sleep(5)
            if i % 200 == 0:
                print(f"{i} visitors; seen/bought/refunds {counts}", flush=True)
            time.sleep(max(0.0, gap - (time.time() - t0)))
    print(f"done: {i} visitors; seen/bought/refunds {counts}", flush=True)


if __name__ == "__main__":
    main()
