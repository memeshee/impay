"""Re-adjudicate invoices whose screens were bought under an older policy.

The paid response is written to `docs/receipts/` before it is parsed, so a policy fix
does not require buying the screen again: this replays what the vendor already said
through the current `services.screen_verdict` and moves each invoice to the status the
current policy licenses. Terminal states (settled / dropped / expired) are untouched,
and a block is only relaxed to a hold — never to payable.

    python scripts/rejudge_screens.py            # dry run
    python scripts/rejudge_screens.py --apply
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

from bot import services, store  # noqa: E402


def _result_for(rec: dict) -> dict | None:
    """The screen's result, re-read from the RAW paid response when it is on disk.

    Old summaries dropped the fields the current policy reads (allTokensCount,
    containsCount, the candidate list), so replaying the summary alone would decide on
    missing data and print an empty reason. The raw body is stored before parsing.
    """
    path = rec.get("receipt_file")
    if path:
        try:
            raw = json.loads(open(path).read())
            body = json.loads(raw.get("text") or "{}")
            if body:
                return services._summarize(rec["slug"], body)
        except (OSError, ValueError):
            pass
    return rec.get("result") or None


def decide(inv: dict) -> tuple[str, str] | None:
    """The harshest decision across the stored sanctions receipts for this invoice."""
    blocked: tuple[str, str] | None = None
    held: tuple[str, str] | None = None
    subject = (inv.get("recipient") or "")
    for rec in inv.get("services") or []:
        if not rec.get("slug", "").startswith("sanctions") or not rec.get("ok"):
            continue
        result = _result_for(rec)
        if not result:
            continue
        decision, why = services.screen_verdict(rec["slug"], result)
        if decision == services.BLOCK and blocked is None:
            blocked = (decision, f"{why} for {subject!r}" if subject else why)
        elif decision == services.HOLD and held is None:
            held = (decision, f"{why} for {subject!r}" if subject else why)
    return blocked or held



def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    changed = 0
    for inv in sorted((store._load().get("invoices") or {}).values(),
                      key=lambda i: i.get("id") or ""):
        if inv.get("status") not in ("blocked", "held"):
            continue
        verdict = decide(inv)
        if not verdict:
            continue
        decision, why = verdict
        want = "blocked" if decision == services.BLOCK else "held"
        if inv.get("status") == want:
            continue
        print(f"{inv['id']}  {inv['status']} -> {want}\n    {why[:160]}")
        if apply:
            store.rejudge(inv["id"], decision=decision, reason=why)
        changed += 1
    print(f"\n{changed} invoice(s) {'re-adjudicated' if apply else 'would change'}"
          f"{'' if apply else ' — re-run with --apply'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
