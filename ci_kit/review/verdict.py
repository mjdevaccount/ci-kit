"""Turn the CI reviewer's structured output into the `review gate` check's pass/fail.

WHY THIS EXISTS (2026-09-26). Two operator laws collided. PRs open READY with auto-merge ARMED
AT OPEN (CI is the merge gate, no human, no session), and "dispatch `reviewer` before merging a
build loop of 3+ commits or any diff adding a GUARD". The second lived only in session memory,
so the first always won: trade-data#2059 added a ruff ban, auto-merged 30s after opening, and
the reviewer's DO NOT SHIP (the ban had no test) arrived after main already had it. The review
could only ever be advisory. The reusable `.github/workflows/review-gate.yml` makes it a CI check, so auto-merge
waits for it the same way it waits for `unit tests`.

The ledger says the check earns its cost: of 134 closed `reviewer` dispatches in trade-data's
`ops.agent_dispatches` (queried 2026-09-26), 41 closed `miss` -- a blocking finding in 31% of the
diffs it was sent. Anthropic's managed Code Review posts findings but explicitly never blocks a
merge, so it cannot be this gate; a reviewer returning this schema can (`agent` / `claude`).

THE POLICY, and why each branch is shaped the way it is:
  * PASS  -- ship is SHIP or SHIP WITH NOTED RISK, and no CONFIRMED blocking finding.
  * BLOCK -- ship is DO NOT SHIP, OR the verdict says SHIP while listing a CONFIRMED blocking
             finding. A self-contradicting review is not a pass: the finding is the evidence and
             the verdict is the summary, and a summary cannot overrule its evidence.
  * BLIND -- no output, unparseable JSON, or a shape the schema should have prevented. The
             reviewer did not finish looking, and BLIND must never read as OK -- the estate's
             signature defect is a check whose failure looks like success. It fails the check.

The script always exits 0 once it has written its outputs; the workflow's last step turns
`result` into the job's colour, after the PR comment and the ntfy push have gone out. Exit 2 means
this script itself could not run (bad arguments), which the workflow also treats as red.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

SHIP_VALUES = ("SHIP", "SHIP WITH NOTED RISK", "DO NOT SHIP")
SEVERITIES = ("blocking", "risk", "note")
_FINDING_TEXT = ("file", "summary", "failure_scenario")  # _finding_md indexes these; missing = BLIND
MARKER = "<!-- review-gate -->"

# The verdict schema the reviewer returns (both transports take it as --schema). Kept here so the
# workflow and the parser cannot drift: the workflow reads it with `--print-schema`.
SCHEMA = {
    "type": "object",
    "properties": {
        "ship": {"type": "string", "enum": list(SHIP_VALUES)},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "verdict": {"type": "string", "enum": ["CONFIRMED", "PLAUSIBLE"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                    "failure_scenario": {"type": "string"},
                },
                "required": ["severity", "verdict", "file", "summary", "failure_scenario"],
            },
        },
        "not_reviewed": {"type": "string"},
    },
    "required": ["ship", "findings", "not_reviewed"],
}


@dataclass(frozen=True)
class Verdict:
    result: str  # pass | block | blind
    title: str
    body: str


def _finding_md(f: dict) -> str:
    where = f"`{f['file']}:{f['line']}`" if f.get("line") else f"`{f['file']}`"
    return (f"- **{f['severity']} / {f['verdict']}** {where}: {f['summary']}\n"
            f"  - failure: {f['failure_scenario']}")


def decide(raw: str | None, reasons: str) -> Verdict:
    head = f"{MARKER}\n### review gate\n\nIn the review tier because: {reasons or '(unknown)'}\n\n"
    try:
        data = json.loads(raw) if raw and raw.strip() else None
    except json.JSONDecodeError as e:
        data, err = None, f"unparseable output ({e.msg} at char {e.pos})"
    else:
        err = "no structured output (the reviewer did not finish)" if data is None else ""
    if data is not None:
        if not isinstance(data, dict) or data.get("ship") not in SHIP_VALUES \
                or not isinstance(data.get("findings"), list) \
                or any(not isinstance(f, dict) or f.get("severity") not in SEVERITIES
                       or f.get("verdict") not in ("CONFIRMED", "PLAUSIBLE")
                       or not all(isinstance(f.get(k), str) for k in _FINDING_TEXT)
                       for f in data["findings"]):
            data, err = None, "output does not match the schema"
    if data is None:
        return Verdict("blind", "review gate BLIND: the reviewer did not finish looking",
                       head + f"**BLIND** -- {err}. This is a red check, not a pass; re-run the "
                              "job, or add the missing credential if the log names one.\n")

    findings = data["findings"]
    confirmed_blocking = [f for f in findings
                          if f["severity"] == "blocking" and f["verdict"] == "CONFIRMED"]
    ship = data["ship"]
    contradicted = ship != "DO NOT SHIP" and bool(confirmed_blocking)
    result = "block" if ship == "DO NOT SHIP" or contradicted else "pass"
    order = {s: i for i, s in enumerate(SEVERITIES)}
    lines = [_finding_md(f) for f in sorted(findings, key=lambda f: order[f["severity"]])]
    body = head + f"**{ship}**"
    if contradicted:
        body += (" -- but the review lists a CONFIRMED blocking finding, so the gate BLOCKS: a "
                 "verdict cannot overrule its own evidence")
    body += "\n\n" + ("\n".join(lines) if lines else "_No findings._") + "\n"
    if data.get("not_reviewed"):
        body += f"\n**Not reviewed:** {data['not_reviewed']}\n"
    n_block = len(confirmed_blocking)
    if result == "pass":
        title = f"review gate passed: {ship}"
    elif n_block:
        title = f"review gate BLOCKED: {n_block} confirmed blocking finding(s)"
    else:  # DO NOT SHIP resting on PLAUSIBLE findings alone: say so, never "0 confirmed"
        title = "review gate BLOCKED: DO NOT SHIP (no CONFIRMED blocker; read the findings)"
    return Verdict(result, title, body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--print-schema", action="store_true")
    ap.add_argument("--reasons", default="")
    ap.add_argument("--body-out")
    args = ap.parse_args(argv)
    if args.print_schema:
        print(json.dumps(SCHEMA, separators=(",", ":")))
        return 0
    if not args.body_out:
        ap.error("--body-out is required")
    v = decide(os.environ.get("REVIEW_JSON"), args.reasons)
    with open(args.body_out, "w", encoding="utf-8") as fh:
        fh.write(v.body)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"result={v.result}\ntitle={v.title}\n")
    print(f"{v.result}: {v.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
