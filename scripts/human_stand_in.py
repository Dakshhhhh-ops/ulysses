"""A stand-in for the owner's browser taps, for unattended and judge runs.

This process plays the human, not the agent. It is separate from
``agent_session.py``, which never sees a key. It watches the contract service
for two things the owner would do in the browser:

1. a drafted contract that needs the owner's signature: it prints what the
   owner is agreeing to, then signs it;
2. a fired proposal waiting for the one-tap confirmation: it prints the card's
   verdict, then confirms it.

It signs with the owner's passphrase-encrypted key file
(``~/.ulysses/keys/<owner>.key``). It reads the contract and card pages with a
read capability minted from the service's database, as the links the service
prints carry. For a recorded demo, use the browser
instead (``agent_session.py`` waits for the tap; open the printed links).

    ULYSSES_KEY_PASSPHRASE_MAYA=... uv run python scripts/human_stand_in.py --owner maya
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ulysses import signing  # noqa: E402
from ulysses.core import Settings, Ulysses  # noqa: E402
from ulysses.flagsmith.fake import FakeFlagsmith  # noqa: E402
from ulysses.store import Store  # noqa: E402

TAG = "\033[1;35m[STAND-IN HUMAN]\033[0m"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="maya")
    ap.add_argument("--service", default=os.environ.get("ULYSSES_PUBLIC_URL", "http://127.0.0.1:8200"))
    ap.add_argument("--db", default=os.environ.get("ULYSSES_DB", ".run/ulysses.db"))
    ap.add_argument("--every", type=float, default=3.0)
    ap.add_argument("--minutes", type=float, default=120)
    a = ap.parse_args()
    url = a.service.rstrip("/")
    kp = signing.load_keypair(a.owner, prompt=True)
    store = Store(ROOT / a.db if not Path(a.db).is_absolute() else a.db)
    links = Ulysses(store, FakeFlagsmith(), Settings(public_url=url))  # mints read capabilities only

    def cap(row: dict) -> dict:
        return {"cap": links.mint_read(links.read_scope(row["environment"], row["experiment_id"]))}

    print(f"{TAG} signing as {a.owner} with an encrypted key file, in place of the browser tap", flush=True)
    done: set[tuple[str, str]] = set()
    tries: dict[str, int] = {}
    end = time.time() + a.minutes * 60
    with httpx.Client(base_url=url, timeout=60) as http:
        while time.time() < end:
            try:
                for row in store.all_contracts():
                    h = row["hash"]
                    if row["state"] == "draft" and ("sign", h) not in done:
                        st = http.get(f"/human/contract/{h}", params=cap(row)).json()
                        if "owner" in st.get("needs", []) and st["summary"]["owner"] == a.owner:
                            s = st["summary"]
                            print(f"{TAG} read contract {h[:19]}: {s['hypothesis']} | {s['horizon']} | "
                                  f"{s['boundary']} | win: {s['on_win']}, horizon: {s['on_horizon']}", flush=True)
                            if (s.get("autonomy") or {}).get("without_confirmation"):
                                print(f"{TAG}   signed autonomy clause: {s.get('autonomy_text')}", flush=True)
                            for w in (st.get("policy") or {}).get("warnings", []):
                                print(f"{TAG}   warning shown to the signer: {w}", flush=True)
                            sig = kp.sign(signing.contract_message(h, "owner"))
                            r = http.post("/human/sign", json={"hash": h, "role": "owner", "signer": a.owner,
                                                               "signature": sig})
                            body = r.json()
                            print(f"{TAG} signed {h[:19]} -> {r.status_code} {body.get('state') or body.get('detail')}",
                                  flush=True)
                            tries[h] = tries.get(h, 0) + 1
                            if r.status_code < 400 or tries[h] >= 5:
                                done.add(("sign", h))
                        else:
                            done.add(("sign", h))
                    for p in store.proposals_for(h):
                        if p["state"] != "pending" or ("confirm", p["id"]) in done:
                            continue
                        ident = p["look_id"] or p["id"]
                        card = http.get(f"/human/card/{ident}", params=cap(row)).json()
                        reason = (card.get("decision") or {}).get("reason") or p["outcome"]
                        print(f"{TAG} card {ident}: {reason}", flush=True)
                        r = http.post("/human/confirm", json={
                            "proposal_id": p["id"], "signer": a.owner,
                            "signature": kp.sign(card["confirm_message"].encode())})
                        body = r.json()
                        print(f"{TAG} one-tap confirm '{p['action']}' -> {r.status_code} "
                              f"{body.get('state') or body.get('detail')}", flush=True)
                        done.add(("confirm", p["id"]))
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                print(f"{TAG} service not answering ({type(exc).__name__}); trying again", flush=True)
            time.sleep(a.every)


if __name__ == "__main__":
    main()
