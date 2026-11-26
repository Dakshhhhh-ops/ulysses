"""What the agent container can and cannot reach. Run inside it; prints one JSON report.

    docker compose -p ulysses --profile agent run --rm --entrypoint python agent \
        scripts/agent_boundary_check.py [--read ENVIRONMENT_KEY:EXPERIMENT_ID]

It tries to read every place a secret lives on the host side of the split (the project's .env, the
service database, the run folder with the judge demo's keys and the demo shop's server-side key, the
signers' key directory) and records each failure; lists the environment variables it was given;
checks that the Flagsmith stack's Postgres name does not resolve; then, over the real MCP stdio
protocol, lists the Ulysses tools, optionally reads one experiment through the service, and asks the
service to start a contract that does not exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

ROOT = Path(__file__).resolve().parents[1]
SECRET_PLACES = [
    ROOT / ".env", ROOT / ".run", ROOT / ".run" / "ulysses.db", ROOT / ".run" / "judge.db",
    ROOT / ".run" / "judge.env", ROOT / ".run" / "demo.json", ROOT / ".run" / "keys",
    Path.home() / ".ulysses", Path.home() / ".ulysses" / "keys", Path("/run/secrets"),
]


def try_read(p: Path) -> dict:
    try:
        if p.is_dir():
            return {"path": str(p), "readable": True, "entries": len(list(p.iterdir()))}
        data = p.read_bytes()
        return {"path": str(p), "readable": True, "bytes": len(data)}
    except OSError as exc:
        return {"path": str(p), "readable": False, "error": type(exc).__name__}


def resolves(name: str) -> bool:
    try:
        socket.gethostbyname(name)
        return True
    except OSError:
        return False


def payload(res) -> dict:
    sc = getattr(res, "structuredContent", None)
    if sc:
        return sc.get("result", sc)
    for block in getattr(res, "content", []) or []:
        if getattr(block, "text", None):
            return json.loads(block.text)
    return {}


async def mcp_probe(read: str | None) -> dict:
    env = get_default_environment()
    env.update({k: os.environ[k] for k in ("ULYSSES_URL", "ULYSSES_AGENT_TOKEN") if k in os.environ})
    params = StdioServerParameters(command=sys.executable, args=["-m", "ulysses.mcp_server"], env=env,
                                   cwd=str(ROOT))
    out: dict = {}
    with open(os.devnull, "w") as quiet:
        async with stdio_client(params, errlog=quiet) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)
            if read:
                environment, _, eid = read.rpartition(":")
                got = payload(await s.call_tool("read_experiment",
                                                {"environment": environment, "experiment_id": int(eid)}))
                exp = got.get("experiment") or {}
                out["read_experiment"] = {"ok": got.get("ok"), "status": exp.get("status"), "flag": exp.get("flag"),
                                          "has_results": got.get("results") is not None,
                                          "untrusted_fields_listed": bool(got.get("untrusted_text"))}
            bogus = "sha256:" + "0" * 64
            st = payload(await s.call_tool("start_under_contract", {"contract_hash": bogus}))
            out["start_unknown_contract"] = {"ok": st.get("ok"), "status": st.get("status")}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--read", help="ENVIRONMENT_KEY:EXPERIMENT_ID to read through the service")
    a = ap.parse_args()
    report = {
        "files": [try_read(p) for p in SECRET_PLACES],
        "env_names": sorted(os.environ),
        "flagsmith_env": sorted(k for k in os.environ if k.startswith("FLAGSMITH_")),
        "resolves_postgres": resolves("postgres"),
        "resolves_flagsmith": resolves("flagsmith"),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
    }
    try:
        report["mcp"] = asyncio.run(mcp_probe(a.read))
    except Exception as exc:  # noqa: BLE001 - report what failed instead of a traceback
        report["mcp"] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
