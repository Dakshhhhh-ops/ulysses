"""Write a replay trace from a finished run's store (format: docs/REPLAY.md).

Reads the store read-only, with ``data-sources.json`` and ``demo.json`` next to it if present, and
writes one JSON object per line: a header, then every recorded event in order with its time as
seconds since the first event. No calendar date or clock time is written; the exporter refuses to.

    uv run python scripts/export_replay.py --db <run dir>/judge.db --out data/replay-<name>.jsonl \
        --title "..." --flagsmith "stock open-source Flagsmith 2.295.2 (docker-compose.yml)"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ulysses.replay import TraceError, export_store, write_trace  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="the finished run's store (for example .run/judge.db)")
    ap.add_argument("--out", required=True, help="where to write the trace (for example data/replay-judge.jsonl)")
    ap.add_argument("--title", required=True, help="one line saying what run this is")
    ap.add_argument("--flagsmith", required=True, help="which Flagsmith the run used")
    ap.add_argument("--note", help="anything a reader of the trace should know")
    a = ap.parse_args()
    try:
        lines = export_store(a.db, title=a.title, flagsmith=a.flagsmith, note=a.note)
    except TraceError as exc:
        sys.exit(f"export stopped: {exc}")
    write_trace(lines, a.out)
    h = lines[0]
    print(f"wrote {a.out}: {len(lines) - 1} events over {h['recorded_span_s']} recorded seconds; counts {h['counts']}; "
          f"not replayed {h['not_replayed']}")


if __name__ == "__main__":
    main()
