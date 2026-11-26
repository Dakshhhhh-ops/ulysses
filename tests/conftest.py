from __future__ import annotations

import pytest

from ulysses import contract as ct
from ulysses.core import Settings, Ulysses
from ulysses.flagsmith.fake import FakeFlagsmith
from ulysses.signing import Keypair
from ulysses.store import Store


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def keys() -> dict[str, Keypair]:
    return {"maya": Keypair.generate(), "sam": Keypair.generate(), "mallory": Keypair.generate()}


def make_contract(keys, *, n=1000, looks=10, alpha=0.05, experiment=1, **kw) -> ct.Contract:
    return ct.draft(
        experiment=ct.ExperimentRef(project=1, environment="env-key", experiment=experiment,
                                    flag="checkout_button", treatment="big_cta"),
        hypothesis="The big button lifts purchases",
        primary_metric=ct.Metric(name="purchase", direction="increase"),
        guardrails=[ct.Guardrail(name="refunds", direction="not_increase")],
        horizon=ct.Horizon(identities_per_arm=n, looks=looks),
        owner=ct.Signer(name="maya", public_key=keys["maya"].public),
        second_signer=ct.Signer(name="sam", public_key=keys["sam"].public),
        alpha=alpha,
        **kw,
    )


@pytest.fixture
def world(clock, keys):
    """A service wired to a fake Flagsmith with two registered signers and one experiment."""
    fs = FakeFlagsmith(clock=clock)
    fs.add_experiment(1)
    store = Store(":memory:")
    svc = Ulysses(store, fs, Settings(public_url="http://test", refresh_wait_seconds=0, poll_seconds=0),
                  clock=clock, sleep=lambda s: clock.advance(s))
    svc.register_signer("maya", keys["maya"].public)
    svc.register_signer("sam", keys["sam"].public)
    return svc, fs, store


def tap(svc, keys, p, who="maya"):
    """The owner's one-tap confirmation over exactly the text the card shows (with the looks' evidence)."""
    return keys[who].sign(svc.proposal_card(p["id"])["confirm_message"].encode())


def read_all(client, svc):
    """Let a test client read every human page, as the operator's overview link does."""
    from ulysses import capability

    client.cookies.set("ulysses_read_all", svc.mint_read(capability.ALL))
    return client


def sign_as(svc, keys, h, who="maya", role="owner"):
    from ulysses.signing import contract_message

    return svc.sign(h, role, who, keys[who].sign(contract_message(h, role)))
