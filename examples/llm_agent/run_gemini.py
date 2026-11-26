"""Run the LLM agent's scenarios with Google Gemini as the model.

This is ``run.py`` with ``GeminiModel`` plugged into its model slot: the same
scenario set-ups, the same real contract service, the same MCP server over
stdio and the same transcript format. Only the model client differs, and the
transcript's ``meta`` event names the Gemini model id, whether Flagsmith was
live or the test double, and the command that produced it.

    uv run --extra gemini python examples/llm_agent/run_gemini.py scenario early-96 --export
    uv run --extra gemini python examples/llm_agent/run_gemini.py scenario all --export
    uv run --extra gemini python examples/llm_agent/run_gemini.py live --with-traffic --export \
        --lab-lock ../flagsmith-lab/LAB-LOCK

The key: GEMINI_API_KEY from the environment, or else loaded at run time from
~/.ulysses/llm.env (outside the repository). It is never printed, and every
transcript is scrubbed of it and of the agent token.
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from examples.llm_agent import gemini_model as gm  # noqa: E402


def command_line(argv: list[str]) -> str:
    return "uv run --extra gemini python examples/llm_agent/run_gemini.py " + " ".join(shlex.quote(a) for a in argv)


def plug_in(run_mod, model_id: str, command: str):
    """Put Gemini into run.py's model slot and its transcripts' meta. Returns the patched module."""
    harness = run_mod.harness
    base = harness.Transcript

    class GeminiTranscript(base):  # type: ignore[misc, valid-type]
        def __init__(self, path, secrets):
            super().__init__(path, [*secrets, *gm.secret_values()])

        def event(self, kind, **data):
            if kind == "meta":
                data["model_client"] = "gemini"
                data["model"] = model_id
                data["command"] = command
                data["key_handling"] = ("GEMINI_API_KEY loaded into the process environment at run time; "
                                        "scrubbed from this transcript; no HTTP headers recorded")
            return super().event(kind, **data)

    harness.Transcript = GeminiTranscript
    run_mod.choose_model = lambda *_a, **_k: gm.GeminiModel(model=model_id)
    return run_mod


class LabLock:
    """The shared lab's lock file: wait while another workstream holds it, hold it only while running."""

    def __init__(self, path: str | None, owner: str = "G", every: float = 150.0):
        self.path = Path(path) if path else None
        self.owner, self.every = owner, every

    def __enter__(self):
        if self.path:
            while self.path.exists():
                print(f"lab lock {self.path} is held ({self.path.read_text(errors='replace').strip()!r}); "
                      f"checking again in {self.every:.0f}s", flush=True)
                time.sleep(self.every)
            self.path.write_text(self.owner, encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if self.path and self.path.exists() and self.path.read_text(errors="replace").strip() == self.owner:
            self.path.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("scenario")
    s.add_argument("name")
    lv = sub.add_parser("live")
    lv.add_argument("--with-traffic", action="store_true")
    lv.add_argument("--shop-port", type=int, default=8210)
    lv.add_argument("--min-lift", type=float, default=0.5)
    lv.add_argument("--baseline-rate", type=float, default=0.10)
    lv.add_argument("--max-turns", type=int, default=200)
    lv.add_argument("--lab-lock", help="the shared lab's LAB-LOCK file, held only while the run lasts")
    for p in (s, lv):
        p.add_argument("--gemini-model", default=gm.DEFAULT_MODEL)
        p.add_argument("--export", action="store_true",
                       help="also write a scrubbed, time-free copy to examples/llm_agent/transcripts/")
    argv = sys.argv[1:]
    a = ap.parse_args(argv)
    if not gm.load_key():
        sys.exit(f"{gm.KEY_VAR} is not set and {gm.KEY_FILE} could not be read")
    from examples.llm_agent import run as run_mod

    plug_in(run_mod, a.gemini_model, command_line(argv))
    print(f"model client: gemini ({a.gemini_model})", flush=True)
    try:
        if a.mode == "live":
            a.model = "gemini"
            with LabLock(a.lab_lock):
                asyncio.run(run_mod.run_live(a))
        else:
            from examples.llm_agent.scenarios import SCENARIOS

            names = list(SCENARIOS) if a.name == "all" else [a.name]
            if hasattr(run_mod, "run_scenarios"):  # one fixture service, one MCP server, each scenario in turn
                asyncio.run(run_mod.run_scenarios(names, "gemini", export=a.export))
            else:
                for n in names:
                    asyncio.run(run_mod.run_scenario(n, "gemini", export=a.export))
    except run_mod.Stop as exc:
        print(f"llm agent stopped: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
