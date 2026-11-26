"""Operator setup for the end-to-end demo (run once, before the agent session).

This is the operator's job, not the agent's. It uses the Flagsmith admin key
from .env to create, in the ``ulysses-demo`` environment:

* a fresh experiment on ``ulysses_checkout_cta`` (classic against bold), where
  the simulated visitors have a real preference, for the "fire and ship" scene;
* a fresh experiment on ``ulysses_headline_aa``, whose two variations show the
  same headline, for the "hold" scene: any winner Flagsmith reports there is noise.

Before creating them it says exactly what it will clear on those flags. It
deletes an unstarted experiment, refuses to end a running one unless you pass
``--replace``, and never ends one a Ulysses contract governs.

It also creates the two demo signers, owner ``maya`` and second signer
``sam``, as passphrase-encrypted key files in your user profile
(``~/.ulysses/keys``, outside this project), and registers their public keys
with the contract service. The passphrases are asked for, or read from
ULYSSES_KEY_PASSPHRASE_MAYA / ULYSSES_KEY_PASSPHRASE_SAM for a scripted run.
In the browser you would instead enrol with ``ulysses signer enrol maya``.

    uv run python scripts/demo_setup.py [--replace]
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from ulysses import lab, signing  # noqa: E402
from ulysses.service.runtime import load_dotenv  # noqa: E402
from ulysses.store import Store  # noqa: E402

SIGNERS = ("maya", "sam")


def ensure_key(name: str) -> signing.Keypair:
    path = signing.key_path(name)
    pw = signing.passphrase_for(name)
    if path.exists():
        return signing.load_keypair(name, passphrase=pw, prompt=pw is None)
    if pw is None:
        pw = getpass.getpass(f"Choose a passphrase for {name}'s signing key: ")
    if len(pw) < 8:
        sys.exit("use a passphrase of at least 8 characters")
    kp = signing.Keypair.generate()
    signing.save_keypair(name, kp, passphrase=pw)
    print(f"created {name}'s signing key at {path} (encrypted, readable only by you)")
    return kp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replace", action="store_true",
                    help="complete (keeping control) an unfinished experiment that no contract governs")
    a = ap.parse_args()
    load_dotenv()
    cfg = lab.LabConfig.from_env()
    demo = lab.setup_demo(cfg)
    store = Store(os.environ.get("ULYSSES_DB", ".run/ulysses.db"))
    governed = {e for _, e in store.governed_experiments()}
    governed |= {r["experiment_id"] for r in store.all_contracts() if r["state"] in ("signed", "starting")}

    made = {}
    for which, name, hyp in (
        ("cta", "ulysses checkout cta", "The bold call to action raises purchases without raising refund requests."),
        ("aa", "ulysses headline A/A", "A/A check: both headline variations are identical, so neither should win."),
    ):
        try:
            out = lab.new_demo_experiment(cfg, name, hyp, which=which, replace=a.replace, governed=governed)
        except RuntimeError as exc:
            sys.exit(f"stopped: {exc}")
        for c in out["cleared"]:
            print(f"{out['flag']}: experiment {c['id']} (was {c['was']}) {c['action']}")
        print(f"{out['flag']}: experiment {out['experiment_id']} created (status {out['status']})")
        made[which] = out["experiment_id"]

    for name in SIGNERS:
        kp = ensure_key(name)
        existing = store.signer(name)
        if existing and existing["public_key"] != kp.public:
            sys.exit(f"signer {name!r} is registered with a different key; enrol again with a new link")
        store.add_signer(name, kp.public, time.time())
    out = {
        "environment": demo["environment_key"], "project_id": demo["project_id"],
        "experiment_id": made["cta"], "flag": demo["flag"],
        "experiments": {"cta": {"id": made["cta"], "flag": demo["flag"], "kind": "real effect (simulated visitors)"},
                        "aa": {"id": made["aa"], "flag": demo["aa"]["flag"], "kind": "A/A"}},
        "owner": "maya", "second_signer": "sam",
    }
    (ROOT / ".run" / "demo.json").write_text(json.dumps(out, indent=2))
    print("signers maya (owner) and sam (second signer) registered; wrote .run/demo.json")


if __name__ == "__main__":
    main()
