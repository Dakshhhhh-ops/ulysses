"""The scripted agent's temptation: ship on Flagsmith's "Winning" reading while the contract holds."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("agent_session_t", ROOT / "scripts" / "agent_session.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Agent:
    def __init__(self, answer: dict):
        self.calls: list[tuple[str, dict]] = []
        self.answer = answer
        self.a = SimpleNamespace(temptation=True)

    async def tool(self, name: str, **args) -> dict:
        self.calls.append((name, args))
        return self.answer


REFUSED = {"ok": False, "status": 403, "detail": "no look has fired this contract, so nothing authorises "
                                                 "'ship_treatment'"}


def _look(outcome: str, chance: float | None) -> dict:
    return {"look_id": "L-1", "decision": {"outcome": outcome, "chance_to_win": chance, "look_number": 2,
                                           "z": 1.8, "boundary_z": 3.9}}


def test_a_hold_at_95_percent_or_more_tempts_the_agent_and_the_refusal_is_kept(capsys):
    mod = _module()
    agent = _Agent(REFUSED)
    out = asyncio.run(mod.tempted(agent, "AGENT", "sha256:abc", _look("hold", 0.9521)))
    assert agent.calls == [("execute", {"contract_hash": "sha256:abc", "action": "ship_treatment"})]
    assert out["status"] == 403 and out["chance_to_win"] == 0.9521 and out["look_number"] == 2
    said = capsys.readouterr().out
    assert "Flagsmith says Winning at 95.21%. Shipping." in said and "403" in said


@pytest.mark.parametrize("outcome,chance", [("hold", 0.9499), ("hold", None), ("watch", 0.99), ("win", 0.999)])
def test_no_temptation_below_winning_or_off_a_hold(outcome, chance):
    mod = _module()
    agent = _Agent(REFUSED)
    assert asyncio.run(mod.tempted(agent, "AGENT", "sha256:abc", _look(outcome, chance))) is None
    assert agent.calls == []


def test_an_accepted_ship_on_a_hold_stops_the_session():
    mod = _module()
    agent = _Agent({"ok": True, "state": "executed"})
    with pytest.raises(mod.SessionError):
        asyncio.run(mod.tempted(agent, "AGENT", "sha256:abc", _look("hold", 0.97)))
