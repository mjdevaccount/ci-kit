"""runs.sh (ci_kit/runners/sh/), the supervisor's ONE reader of CI demand: memo_get, runs_read,
recent_merge, open_runs, job_demand -- run in real bash and jq with the PACKAGED file sourced and
gh_get stubbed.

What these pin: every page up to RUN_PAGES is read and what lies past it is counted (UNREAD), never
silently capped; one URL is read once per tick; a failed page fails the read; a run a status filter
dropped is merged back from the unfiltered newest page (LAG), and a page that cannot be read or
parsed is LAG -1 with demand left as the filters read it; a pending run with a queued job is demand.

bash or jq missing FAILS in CI (`$CI` set) and skips only on a workstation without them.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess

import pytest

from ci_kit.runners.scripts import SH_DIR

LIB = SH_DIR / "runs.sh"
_R = "/repos/o/r/actions/runs"
# jq.exe on a Windows workstation ends lines CRLF; a no-op on Linux. pipefail as a supervisor runs,
# so the stripping pipe cannot hide a jq failure.
_PRE = "set -uo pipefail\njq() { command jq \"$@\" | tr -d '\\r'; }\n"


def _bash() -> str:
    bash = shutil.which("bash")  # resolved: a bare "bash" on Windows is System32's WSL launcher
    if bash is None or shutil.which("jq") is None:
        if os.environ.get("CI"):
            pytest.fail("bash and jq must be on PATH: a supervisor cannot run without them")
        pytest.skip("bash/jq not on PATH (workstation)")
    return bash


def _sh(body: str) -> str:
    script = _PRE + f". {shlex.quote(LIB.as_posix())}\nOWNER=o\nCALLS=$(mktemp)\n" + body
    return subprocess.run([_bash(), "-c", script], capture_output=True, text=True, check=True).stdout


# gh_get stub: TOTAL runs, newest first, 100 a page; ids TOTAL..1. CALLS counts every request in a
# file (gh_get runs in a $( ) subshell, so a variable would not survive). FAIL_PAGE fails one page;
# DUP shifts a page boundary by one run (a run created mid-read); CLAIM overrides total_count.
_PAGES = r"""
gh_get() {
  echo "$1" >> "${CALLS}"
  local p=1
  [[ "$1" =~ [\&?]page=([0-9]+) ]] && p="${BASH_REMATCH[1]}"
  [ "${p}" = "${FAIL_PAGE:-0}" ] && return 1
  jq -nc --argjson t "${TOTAL}" --argjson p "${p}" --argjson dup "${DUP:-0}" --argjson claim "${CLAIM:-null}" \
    '{total_count: ($claim // $t), workflow_runs: [range(($t - ($p - 1) * 100 + $dup); ([($t - $p * 100), 0] | max); -1)
       | select(. >= 1 and . <= $t) | {id: ., created_at: "2026-01-01T00:00:00Z"}]}'
}
"""
_READ = 'if runs_read r queued; then echo "n $(jq ".workflow_runs|length" <<<"$RUNS") unread $UNREAD"; else echo failed; fi'


def _pages(total: int, body: str, fail_page: int = 0, dup: int = 0, claim: str = "null") -> list[str]:
    return _sh(f"TOTAL={total}\nFAIL_PAGE={fail_page}\nDUP={dup}\nCLAIM={claim}\n" + _PAGES + body
               + '\necho "calls $(wc -l < "${CALLS}")"\n').split()


@pytest.mark.parametrize(("total", "want"), [
    (71, ["n", "71", "unread", "0", "calls", "1"]),     # one page is one read
    (250, ["n", "250", "unread", "0", "calls", "3"]),   # every page up to the cap, the young ones included
    (420, ["n", "300", "unread", "120", "calls", "3"]),  # past the cap: counted, never silent
    (100, ["n", "100", "unread", "0", "calls", "1"]),   # got == total_count: no empty page 2
])
def test_runs_read_pages(total, want):
    assert _pages(total, _READ) == want


def test_a_run_shifted_between_pages_counts_once():
    assert _pages(150, _READ, dup=1)[:2] == ["n", "150"]


def test_a_short_page_is_the_end_whatever_total_count_says():
    assert _pages(150, _READ, claim="400") == ["n", "150", "unread", "0", "calls", "2"]


def test_a_failed_page_fails_the_read():
    assert _pages(250, _READ, fail_page=2)[0] == "failed"


def test_sibling_pools_share_one_read():
    assert _pages(250, "\n".join([_READ] * 3))[-2:] == ["calls", "3"]


def _runs(*pairs: tuple[int, str]) -> str:
    return json.dumps({"total_count": len(pairs), "workflow_runs": [
        {"id": i, "status": s, "created_at": "2026-01-01T05:39:31Z"} for i, s in pairs]})


def _merge(recent: str, queued: str, active: str, fail: int = 0, reads: int = 1) -> list[str]:
    show = ('echo "lag $LAG q $(jq -c "[.workflow_runs[].id]" <<<"$queued_runs")'
            ' a $(jq -c "[.workflow_runs[].id]" <<<"$active_runs")"')
    body = (f"FAIL={fail}\nRECENT={shlex.quote(recent)}\nqueued_runs={shlex.quote(queued)}\n"
            f"active_runs={shlex.quote(active)}\n"
            'gh_get() { echo "$1" >> "${CALLS}"; [ "${FAIL}" = 1 ] && return 1; printf \'%s\' "${RECENT}"; }\n'
            + "\n".join(["recent_merge r; " + show] * reads) + '\necho "calls $(wc -l < "${CALLS}")"\n')
    return _sh(body).split()


@pytest.mark.parametrize(("recent", "queued", "active", "want"), [
    # `queued` in the plain list, absent from ?status=queued: it becomes demand
    (_runs((7, "queued"), (6, "completed")), _runs(), _runs(), ["lag", "1", "q", "[7]", "a", "[]"]),
    # a dropped in_progress run goes to the active list
    (_runs((7, "in_progress")), _runs(), _runs(), ["lag", "1", "q", "[]", "a", "[7]"]),
    # runs the filters hold change nothing (an in_progress run must NOT reach queued_runs)
    (_runs((9, "in_progress"), (8, "queued"), (7, "completed")), _runs((8, "queued")), _runs((9, "in_progress")),
     ["lag", "0", "q", "[8]", "a", "[9]"]),
    # open statuses the filters hold are no lag
    (_runs((7, "waiting"), (6, "pending"), (5, "requested")), _runs((7, "waiting"), (6, "pending"), (5, "requested")),
     _runs(), ["lag", "0", "q", "[7,6,5]", "a", "[]"]),
    # dropped not-started runs are merged as not started, and are lag
    (_runs((7, "waiting"), (6, "pending"), (5, "requested")), _runs(), _runs(), ["lag", "3", "q", "[7,6,5]", "a", "[]"]),
])
def test_recent_merge(recent, queued, active, want):
    assert _merge(recent, queued, active) == [*want, "calls", "1"]


def test_an_unread_page_is_minus_one_and_leaves_demand():
    assert _merge("", _runs((8, "queued")), _runs(), fail=1) == ["lag", "-1", "q", "[8]", "a", "[]", "calls", "1"]


@pytest.mark.parametrize("body", ["<html>", "null"])
def test_an_unparsable_page_is_minus_one_and_leaves_demand(body):
    assert _merge(body, _runs((8, "queued")), _runs())[:6] == ["lag", "-1", "q", "[8]", "a", "[]"]


def test_sibling_pools_share_one_read_and_one_parse():
    assert _merge(_runs((7, "queued")), _runs(), _runs(), reads=3)[-2:] == ["calls", "1"]


# open_runs over every open status's filtered list (FX: path -> body; a path absent fails its read),
# then job_demand over each queued run's jobs -- the read a supervisor's demand loop makes
def _lists(**by_status: list[int]) -> dict[str, str]:
    fx = {}
    for s in ("queued", "in_progress", "pending", "waiting", "requested"):
        runs = [{"id": i, "status": s, "created_at": "2026-01-01T21:58:27Z"} for i in by_status.get(s, [])]
        fx[f"{_R}?status={s}&per_page=100"] = json.dumps({"total_count": len(runs), "workflow_runs": runs})
    fx[f"{_R}?per_page=100"] = json.dumps({"workflow_runs": []})
    return fx


def _jobs(rid: int, *labels: list[str], status: str = "queued") -> dict[str, str]:
    jobs = [{"status": status, "created_at": "2026-01-01T21:59:00Z", "labels": lb} for lb in labels]
    return {f"{_R}/{rid}/jobs?filter=latest&per_page=100": json.dumps({"jobs": jobs})}


_POOL = '["self-hosted","ci-heavy"]'


def _demand(fx: dict[str, str]) -> dict[str, str]:
    decl = " ".join(f"[{shlex.quote(k)}]={shlex.quote(v)}" for k, v in fx.items())
    body = (
        f"declare -A FX=({decl})\nnow=$(date -d 2026-01-01T23:00:00Z +%s)\n"
        'gh_get() { [ -n "${FX[$1]+set}" ] || return 1; printf \'%s\' "${FX[$1]}"; }\n'
        'open_runs r; rc=$?\nqjobs=0; wait=0\n'
        # every open run, queued or active, once (a started run can still hold a queued job)
        'for rid in $(jq -rs "[.[].workflow_runs[]] | unique_by(.id) | .[].id" <<<"${queued_runs}${active_runs}"); do\n'
        '  memo_get "/repos/o/r/actions/runs/${rid}/jobs?filter=latest&per_page=100" || continue\n'
        f'  read -r n w < <(job_demand "${{MEMO[/repos/o/r/actions/runs/${{rid}}/jobs?filter=latest&per_page=100]}}" '
        f"{shlex.quote(_POOL)} ci-heavy \"$now\")\n"
        '  qjobs=$((qjobs + n)); [ "$w" -gt "$wait" ] && wait=$w\n'
        "done\n"
        'echo "rc=${rc} qjobs=${qjobs} wait=${wait} lag=${LAG} unread=${UNREAD}'
        ' q=$(jq -c "[.workflow_runs[].id]" <<<"$queued_runs") a=$(jq -c "[.workflow_runs[].id]" <<<"$active_runs")"\n'
    )
    return dict(kv.split("=", 1) for kv in _sh(body).split())


def test_a_pending_run_with_a_queued_job_is_demand():
    # a run `pending` behind a concurrency group while its job is `queued`: a run's status is not demand
    got = _demand({**_lists(pending=[6]), **_jobs(6, ["self-hosted", "ci-heavy"])})
    assert (got["rc"], got["qjobs"], got["q"], int(got["wait"])) == ("0", "1", "[6]", 3600 + 60)


@pytest.mark.parametrize("status", ["waiting", "requested"])
def test_waiting_and_requested_are_read_too(status):
    got = _demand({**_lists(**{status: [7]}), **_jobs(7, ["self-hosted", "ci-heavy"])})
    assert (got["qjobs"], got["q"]) == ("1", "[7]")


def test_a_run_in_two_lists_is_counted_once_and_active_wins():
    got = _demand({**_lists(queued=[5], pending=[5], in_progress=[5]), **_jobs(5, ["self-hosted", "ci-heavy"])})
    assert (got["qjobs"], got["q"], got["a"]) == ("1", "[]", "[5]")


def test_a_concurrency_wait_with_no_queued_job_is_not_demand():
    got = _demand({**_lists(pending=[9]), **_jobs(9, ["self-hosted", "ci-heavy"], status="waiting")})
    assert (got["qjobs"], got["wait"]) == ("0", "0")


def test_a_job_asking_for_a_label_the_pool_lacks_is_not_its_demand():
    got = _demand({**_lists(queued=[4]), **_jobs(4, ["self-hosted", "ci-heavy", "gpu"])})
    assert got["qjobs"] == "0"


def test_one_unreadable_list_is_rc_1_and_keeps_what_was_read():
    fx = {**_lists(queued=[5]), **_jobs(5, ["self-hosted", "ci-heavy"])}
    del fx[f"{_R}?status=pending&per_page=100"]
    got = _demand(fx)
    assert (got["rc"], got["q"], got["qjobs"]) == ("1", "[5]", "1")


def test_every_open_status_is_read():
    assert _sh('echo "${OPEN_STATUSES}"').split() == ["queued", "in_progress", "pending", "waiting", "requested"]
