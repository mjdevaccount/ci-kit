"""The phases of one merge-train pass: finalize a merged train, judge (and land) the open one,
compose the next. The pass itself, its order and its exit codes are cli.py's.

AUTHORITY (ci-kit#70 finding 1/8). The CLI acts only on a train it composed: the PR's composition
record holds against GitHub (github.composed) AND its author is this token's user. Any other
`train/` PR is never judged, merged or closed here -- it is named, red, every pass -- and the review
gate blocks it on the same record.

LANDING (finding 4). A train is NOT armed for auto-merge: GitHub would land it the moment its
checks went green, with no look at whether its riders still want to land. When the train is green
the CLI re-verifies every rider AT LAND TIME -- still open, still armed, not a draft, no
`autoland-off`, head == the head that rode, its review still passing at that head on this base --
and only then merges it (`--rebase --match-head-commit`). A rider that fails is ejected with the
reason (one whose head moved is only released) and the train closed; the rest recompose. A green train GitHub refuses to merge (a ruleset
hold, a required check this caller did not list) is an ERROR, not a warning: nothing lands until
someone looks.
"""
from __future__ import annotations

from pathlib import Path

from ci_kit.merge_train import (
    COMMENT_MARK,
    LABELS,
    LANDED,
    NEEDS_UPDATE,
    OPT_OUT,
    RIDING,
    TRAIN_LABEL,
    TRAIN_PREFIX,
    github,
    policy,
)
from ci_kit.merge_train import git as tgit
from ci_kit.merge_train.ops import (
    Opts,
    Pass,
    age_min,
    close,
    comment,
    comments_of,
    fetch,
    fetch_head,
    label,
)
from ci_kit.review.verdict import MARKER

LANDED_MARK = "<!-- merge-train:landed train={train} head={head} sha={sha} -->"
MOVED_MARK = "<!-- merge-train:moved train={train} head={head} -->"
SKIPPED_MARK = "<!-- merge-train:skipped head={head} -->"
EJECT_MARK = "<!-- merge-train:ejected head={head} -->"


def _once(o: Opts, p: Pass, n: int, mark: str, text: str) -> bool:
    """Comment `text` + `mark` on #n unless a comment already carries `mark` (a re-run is quiet)."""
    if not o.dry_run and any(mark in b for b in comments_of(o.repo, n)):
        return True
    return comment(o, p, n, f"{text}\n{mark}")


def eject(o: Opts, p: Pass, n: int, head: str, why: str) -> None:
    _once(o, p, n, EJECT_MARK.format(head=head),
          f"Ejected from merge trains: {why}. This PR lands through its own auto-merge, tested alone against main.")
    label(o, p, n, add=policy.EJECTED, remove=RIDING)


def authority(o: Opts, train: dict, me: str) -> str:
    """'' when open train PR `train` is one this workflow composed with this token, else why not."""
    pr = github.pull(o.repo, int(train["number"]))
    why = github.composed(o.repo, pr, o.train_workflow)
    if not why and str((pr.get("user") or {}).get("login") or "") != me:
        why = f"opened by {(pr.get('user') or {}).get('login')}, not this workflow's token user {me}"
    return why


# ── 1. finalize ────────────────────────────────────────────────────────────────────────────────
def _moved(d: Path, n: int, h: str, cur: str) -> bool:
    """Did #n's head move FORWARD from `h` (a descendant), rather than get rewritten?"""
    try:
        fetch_head(d, n, cur)
    except Exception:  # noqa: BLE001 -- unknown is reported as "rewritten", the louder message
        return False
    return tgit.git(d, "merge-base", "--is-ancestor", h, cur, check=False).returncode == 0


def finalize(o: Opts, p: Pass, d: Path) -> None:
    """Close each rider of a merged train as landed. The train is labelled `landed-via-train` only
    when every rider is settled: a close that failed is retried by the next pass."""
    merged = github.gh_json("pr", "list", "-R", o.repo, "--state", "merged", "--label", TRAIN_LABEL,
                            "--limit", "20", "--json", "number,mergeCommit,labels,mergedAt")
    for t in merged if isinstance(merged, list) else []:
        if LANDED in {lb.get("name") for lb in t.get("labels") or []} or age_min(t.get("mergedAt")) > 7 * 24 * 60:
            continue
        m = int(t["number"])
        tip = (t.get("mergeCommit") or {}).get("oid") or ""
        msgs = github.gh_json("api", f"repos/{o.repo}/pulls/{m}/commits?per_page=100", "--jq", "[.[].commit.message]")
        rode = [b for b in (tgit.binding(str(x)) for x in (msgs or [])) if isinstance(b, tuple)]
        fetch(d, f"+refs/heads/{o.base}:refs/remotes/origin/{o.base}")
        where = tgit.landed(d, tip, set(rode)) if tip and tgit.has_commit(d, tip) else {}
        ok = bool(rode)
        if not rode:
            p.error(f"train #{m} merged but none of its commits carry a binding: nothing was closed")
        for n, h in rode:
            sha = where.get(n) or tip  # a train merged by squash: every rider landed in its one commit
            pr = github.pull(o.repo, n)
            cur = str((pr.get("head") or {}).get("sha") or "")
            if pr.get("state") != "open":
                continue
            if cur == h:
                ok &= _once(o, p, n, LANDED_MARK.format(train=m, head=h, sha=sha),
                            f"Landed as {sha} via merge train #{m} (this PR's head {h}).")
                if close(o, p, n):
                    label(o, p, n, add=LANDED, remove=RIDING)
                    p.say(f"#{n}: landed as {sha[:12]} via train #{m}, closed")
                else:
                    ok = False
                continue
            if _moved(d, n, h, cur):
                text = (f"Merge train #{m} landed this PR at {h} as {sha}. Commits were added since (head now "
                        f"{cur}): those are NOT on {o.base}, so this PR stays open with them. Its own diff "
                        f"against {o.base} is now only those commits.")
            else:
                text = (f"Merge train #{m} landed this PR at {h} as {sha}. The head was REWRITTEN since "
                        f"(force-pushed to {cur}, not a descendant of {h[:12]}): what landed is {h[:12]}'s "
                        f"content. This PR stays open; diff it against {o.base} before landing anything more.")
            ok &= _once(o, p, n, MOVED_MARK.format(train=m, head=h), text)
            label(o, p, n, remove=RIDING)
            p.say(f"#{n}: train #{m} landed {h[:12]}; head moved to {cur[:12]}, left open")
        if ok and not o.dry_run:
            label(o, p, m, add=LANDED)
            p.say(f"train #{m}: finalized")
        elif rode:
            p.error(f"train #{m}: merged but not every rider was settled; the next pass retries")


# ── 2. judge the open train ────────────────────────────────────────────────────────────────────
def _gate_said(repo: str, m: int) -> str:
    """'blind' | 'block' | '' from the review gate's sticky comment on train #m."""
    for b in comments_of(repo, m):
        if b.lstrip().startswith(MARKER):
            return "blind" if "**BLIND**" in b else "block" if "**BLOCKED" in b else ""
    return ""


def _release(o: Opts, p: Pass, m: int, rode: list[tuple[int, str, str]], why: str, text: str) -> None:
    for n, h, _ in rode:
        comment(o, p, n, f"{text}\n{policy.RELEASED.format(train=m, head=h, why=why)}")
        label(o, p, n, remove=RIDING)


def recheck(o: Opts, n: int, h: str) -> tuple[str, str]:
    """('', '') when rider #n may still land at `h`, else ('release' | 'eject', why) (LANDING, above).
    A moved head is RELEASED, not ejected: the new head is simply not what rode, and a new head is
    what re-admits a PR anyway (round 2, finding 4). Raises when GitHub cannot answer."""
    pr = github.pull(o.repo, n)
    cur = str((pr.get("head") or {}).get("sha") or "")
    title = str(pr.get("title") or "").lower()
    names = {str(lb.get("name")) for lb in pr.get("labels") or []}
    if pr.get("state") != "open":
        return "eject", f"it is {pr.get('state')}"
    if pr.get("draft"):
        return "eject", "it was turned back into a draft"
    if not pr.get("auto_merge"):
        return "eject", "its auto-merge was disarmed"
    if OPT_OUT in names or OPT_OUT in title:
        return "eject", f"it opted out (`{OPT_OUT}`)"
    if cur != h:
        return "release", f"its head moved to {cur[:12]} after it boarded at {h[:12]}"
    if o.review_workflow:
        ok, why, _ = github.review_passed(o.repo, o.review_workflow, n, h, o.base, pr)
        if not ok:
            return "eject", f"its review no longer passes at {h[:12]}: {why}"
    return "", ""


def land(o: Opts, p: Pass, train: dict, rode: list[tuple[int, str, str]]) -> tuple[bool, set[int], frozenset[int]]:
    m, head = int(train["number"]), str(train["headRefOid"])
    bad = [(n, h, *got) for n, h, _ in rode if (got := recheck(o, n, h))[0]]
    if bad:
        for n, h, act, why in bad:
            if act == "eject":
                eject(o, p, n, h, f"at land time merge train #{m} re-checked it and {why}")
            else:
                comment(o, p, n, f"Released from merge train #{m} at land time: {why}. Its new head rides a later "
                                 f"train once its checks pass.\n{policy.RELEASED.format(train=m, head=h, why='moved')}")
                label(o, p, n, remove=RIDING)
        out = {n for n, *_ in bad}
        for n, _, _ in rode:
            if n not in out:
                comment(o, p, n, f"Merge train #{m} was green but did not land: "
                                 f"{', '.join(f'#{x}' for x in sorted(out))} failed the land-time re-check. "
                                 "This PR rides the next train.")
                label(o, p, n, remove=RIDING)
        comment(o, p, m, "Closed at land time: " + "; ".join(f"#{n} {why}" for n, _, _, why in bad))
        close(o, p, m, delete_branch=True)
        p.say(f"train #{m}: green, but {len(bad)} rider(s) failed the land-time re-check, recomposing: "
              + "; ".join(f"#{n} {act}ed ({why})" for n, _, act, why in bad))
        return False, set(), frozenset(out)
    numbers = {n for n, _, _ in rode}
    if o.dry_run:
        p.say(f"(dry run) would land train #{m} at {head[:12]}")
        return True, numbers, frozenset()
    r = github.gh("pr", "merge", str(m), "-R", o.repo, "--rebase", "--match-head-commit", head, check=False)
    if r.returncode:
        p.error(f"train #{m} is green and every rider re-verified, but GitHub refused to merge it: "
                f"{(r.stderr or r.stdout).strip()[:300]}. A ruleset, branch protection or an unlisted required "
                "check is holding it; nothing in it lands until that is resolved")
    else:
        p.say(f"train #{m}: every rider re-verified, landed at {head[:12]} (finalized next pass)")
    return True, numbers, frozenset()


def judge(o: Opts, p: Pass, d: Path, train: dict, main_sha: str) -> tuple[bool, set[int], frozenset[int]]:
    """(stop this pass?, PR numbers still riding, PR numbers not to compose this pass)."""
    m, head = int(train["number"]), str(train["headRefOid"])
    fetch(d, f"+refs/pull/{m}/head:refs/remotes/train/{m}")
    root = tgit.out(d, "merge-base", main_sha, head)
    rode = tgit.riders_of(d, root, head)
    numbers = [n for n, _, _ in rode]
    ph, names = policy.phase(train, root, main_sha, age_min(train.get("createdAt")), o.stale_minutes, o.required)
    p.say(f"train #{m} ({len(rode)} PRs: {', '.join('#' + str(n) for n in numbers)}): {ph}"
          + (f" -- {', '.join(names[:6])}" if names else ""))
    if ph == "pending":
        return True, set(numbers), frozenset()
    if ph == "green":
        return land(o, p, train, rode)
    checks = ", ".join(names[:6])
    if ph == "red":
        said = _gate_said(o.repo, m)
        if policy.red_is_infra(train.get("statusCheckRollup"), o.required) or (said and len(names) == 1):
            why = f"the review gate was {said.upper()} on it" if said else f"{checks} never ran (machinery, not code)"
            _release(o, p, m, rode, "retry", f"Merge train #{m} went red but {why}: no evidence against any PR. "
                                             "This PR is recomposed into a fresh train.")
            comment(o, p, m, f"Closed: red without evidence ({why}). Its PRs are recomposed, none ejected.")
            close(o, p, m, delete_branch=True)
            p.error(f"train #{m}: {why}; riders released for a retry (held after {policy.RETRY_LIMIT} at one head)")
            return False, set(), frozenset()
        keep, out = policy.split(numbers)
        for n, h, _ in rode:
            if n in out:
                eject(o, p, n, h, f"merge train #{m} went red on **{checks}** ({train.get('url')})")
            else:
                comment(o, p, n, f"Merge train #{m} went red on **{checks}**. Bisecting: this PR is in the "
                                 "older half and rides the next train without the newer half.")
                label(o, p, n, remove=RIDING)
        comment(o, p, m, f"Red on **{checks}**. Bisect: keep {keep or 'none'}, eject {out} "
                         "(ejected PRs land on their own auto-merge).")
        close(o, p, m, delete_branch=True)
        p.say(f"train #{m}: red, closed; ejected {out}, kept {keep}")
        return False, set(), frozenset(out)
    # stale / stuck: no evidence against any PR -- release them all, counted per rider
    why = names[0] if ph == "stale" else f"checks never finished after {o.stale_minutes:.0f} min: {checks}"
    _release(o, p, m, rode, ph, f"Merge train #{m} closed ({ph}: {why}). This PR is recomposed into a fresh "
                                f"train; after {policy.RECOMPOSE_LIMIT} such releases at one head it is ejected.")
    comment(o, p, m, f"Closed ({ph}): {why}. Its PRs are recomposed into a fresh train.")
    close(o, p, m, delete_branch=True)
    if ph == "stuck":
        p.error(f"train #{m} was stuck ({why}); a check that never reports will stall every train")
    return False, set(), frozenset()


# ── 4. compose ─────────────────────────────────────────────────────────────────────────────────
def board(o: Opts, p: Pass, d: Path, cands: list[dict]) -> list[tgit.Rider]:
    """The riders for a train: each candidate's own history (rider_gate), head and review re-read."""
    riders: list[tgit.Rider] = []
    for c in cands:
        if len(riders) >= o.max_batch:
            break
        n, h = int(c["number"]), str(c["headRefOid"])
        act, why = policy.rider_gate(comments_of(o.repo, n), h)
        if act == "hold":
            p.error(f"#{n}: held out of trains -- {why}")
            continue
        fetch_head(d, n, h)
        pr = github.pull(o.repo, n)
        if (pr.get("head") or {}).get("sha") != h:
            p.say(f"#{n}: head moved while composing, left for the next pass")
            continue
        at = ""
        if o.review_workflow:
            ok, why, at = github.review_passed(o.repo, o.review_workflow, n, h, o.base, pr)
            if not ok:
                p.say(f"#{n}: not riding -- {why}")
                continue
        riders.append(tgit.Rider(n, h, str(pr.get("title") or ""), str(pr.get("body") or ""), at))
    return riders


def compose(o: Opts, p: Pass, d: Path, cands: list[dict], main_sha: str, me: tgit.Identity) -> None:
    riders = board(o, p, d, cands)
    if len(riders) < o.min_batch:
        p.say(f"{len(riders)} PR(s) ready to ride (< {o.min_batch}): no train; auto-merge lands a single PR")
        return
    tip, included, skipped = tgit.compose(d, main_sha, riders, me)
    for r, why in skipped:
        p.say(f"#{r.number}: skipped -- {why}")
        if why == tgit.CONTEXT_MOVED:  # pr-autoupdate updates it even while trains run (round 2, finding 3)
            label(o, p, r.number, add=NEEDS_UPDATE)
        _once(o, p, r.number, SKIPPED_MARK.format(head=r.head),
              f"Not in this merge train: {why}. It lands through its own auto-merge, or rides a later train.")
    if len(included) < o.min_batch:
        p.say(f"{len(included)} PR(s) composed cleanly (< {o.min_batch}): no train")
        return
    branch = f"{TRAIN_PREFIX}{o.run_id}"
    rows = "\n".join(f"| #{r.number} | {r.title[:80]} | `{r.head}` | `{sha[:12]}` | passed at head ({r.reviewed_at}) |"
                     for r, sha in included)
    body = (f"{COMMENT_MARK}\nMerge train: {len(included)} armed PRs tested ONCE on one tree and landed by "
            f"**rebase** merge, one commit per PR (ci-kit ci-merge-train, run {o.run_id}).\n\n"
            f"Root: `{main_sha}` ({o.base}).\n\n| PR | title | head that rode | train commit | review |\n"
            f"|---|---|---|---|---|\n{rows}\n\n"
            + ("Skipped:\n" + "\n".join(f"- #{r.number}: {why}" for r, why in skipped) + "\n\n" if skipped else "")
            + "Not armed: the merge-train workflow lands this itself once it is green AND every PR above still "
              "wants to land at the head that rode (open, armed, not a draft, no autoland-off, review passing). "
              "The review gate does not re-review this diff: it verifies every commit is exactly its PR's "
              "reviewed head (ci-kit ci_kit/review/train.py). Do not push to this branch.\n\n"
            + f"<!-- merge-train:composed run={o.run} branch={branch} by={me.name} -->\n")
    title = "merge train: " + " ".join(f"#{r.number}" for r, _ in included)
    if o.dry_run:
        p.say(f"(dry run) would open {branch} at {tip[:12]}: {title}")
        return
    tgit.git(d, "push", "-q", "origin", f"{tip}:refs/heads/{branch}")
    for name, (color, desc) in LABELS.items():
        github.gh("label", "create", name, "-R", o.repo, "--color", color, "--description", desc, check=False)
    r = github.gh("pr", "create", "-R", o.repo, "--base", o.base, "--head", branch, "--title", title,
                  "--body-file", "-", "--label", TRAIN_LABEL, check=False, input_text=body)
    if r.returncode:
        p.error(f"open the train PR for {branch}: {(r.stderr or r.stdout).strip()[:300]}")
        tgit.git(d, "push", "-q", "origin", f":refs/heads/{branch}", check=False)
        return
    url = r.stdout.strip().splitlines()[-1]
    m = int(url.rstrip("/").rsplit("/", 1)[-1])
    for rr, sha in included:
        label(o, p, rr.number, add=RIDING, remove=NEEDS_UPDATE)
        comment(o, p, rr.number, f"Riding merge train #{m} ({url}) at head `{rr.head}` as `{sha[:12]}`. It lands as "
                                 "its own commit when the train is green and it still wants to land; pr-autoupdate "
                                 f"leaves it alone meanwhile.\n{policy.RODE.format(train=m, head=rr.head)}")
    p.say(f"train #{m} opened: {title} ({url})")
