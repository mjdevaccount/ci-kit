"""A first-party package bump is never held by the weekly grouping, weekend schedule or age wait.

ci-pin-drift goes red the day a kit releases. If Renovate holds that bump (3-day minimumReleaseAge,
weekend schedule, the weekly "python (non-major)" group), pin-drift stays red until a session
bumps by hand. 2026-11-04: a consumer moved a kit to PyPI, its depName became the bare name,
the name-keyed "first-party kits" rule stopped matching, and v1.1.0 sat pending while pin-drift
was red. Renovate applies packageRules in order and a later match wins, so the rule matched by
source URL must come AFTER every rule that groups, schedules or age-gates a registry manager.

v1.35.0: "never held" is now "held at most a day": every first-party rule shares ONE daily window
(KIT_WINDOW), so a day's releases fold into one PR per kit per repo; pin-drift's grace is 48h.
2026-11-20: the window is 00:00-11:59 ET, not 04:00-07:59 -- a repo Mend runs only a few times a day
missed the four-hour window four days running (MEASURED_RUNS_ET below).
Lock-file maintenance is ONE weekly window and never touched outside it; security is any time.
"""
from __future__ import annotations

import pathlib

import json5

PRESET = pathlib.Path(__file__).resolve().parent.parent / "renovate" / "default.json5"
HOLDS = ("groupName", "schedule", "minimumReleaseAge")
KIT_WINDOW = ["* 0-11 * * *"]   # every day, 00:00-11:59 America/New_York
REGISTRY_MANAGERS = {"pep621", "pip_requirements", "pip-compile", "npm", "nuget"}


def _rules() -> list[dict]:
    return json5.loads(PRESET.read_text(encoding="utf-8"))["packageRules"]


def _source_rule() -> tuple[int, dict]:
    # the any-repo rule; the queue-repo kit rule (rebaseWhen never) shares the URL but holds nothing
    found = [(i, r) for i, r in enumerate(_rules())
             if "https://github.com/mjdevaccount/**" in r.get("matchSourceUrls", []) and not r.get("matchRepositories")]
    assert len(found) == 1, f"expected ONE first-party matchSourceUrls rule, found {len(found)}"
    return found[0]


def test_the_source_url_rule_releases_every_hold() -> None:
    _, rule = _source_rule()
    assert rule["groupName"] is None
    assert rule["schedule"] == KIT_WINDOW
    assert rule["minimumReleaseAge"] is None
    # only the source URL: a name or manager filter would re-open the gap it closes
    assert set(rule) - {"description"} == {"matchSourceUrls", *HOLDS}


def test_no_later_rule_puts_a_hold_back_on_a_registry_manager() -> None:
    at, _ = _source_rule()
    # a later rule re-holds a kit when it sets a hold AND reaches registry packages: a registry
    # manager (or no manager filter) and no package list, or a package list naming a kit
    later = [r for r in _rules()[at + 1:]
             if r not in _kit_groups()
             and any(k in r and r[k] is not None for k in HOLDS)
             and (not r.get("matchManagers") or set(r["matchManagers"]) & REGISTRY_MANAGERS)
             and (not r.get("matchPackageNames")
                  or any("mjdevaccount" in str(p).lower() or str(p) == "llm-ports" for p in r["matchPackageNames"]))]
    assert not later, f"a rule after the first-party source rule re-holds registry packages: {later}"


def test_the_source_rule_comes_after_the_python_group() -> None:
    at, _ = _source_rule()
    python_group = [i for i, r in enumerate(_rules()) if r.get("groupName") == "python (non-major)"]
    assert python_group and max(python_group) < at


KITS = ("ci-kit", "edge-platform", "llm-ports", "market-ports")
NAME_MATCHERS = ("matchPackageNames", "matchDepNames", "matchSourceUrls")
KIT_MANAGERS = {"github-actions", "pep621", "custom.regex", "regex", "pip_requirements", "pip-compile"}


def _kit_groups() -> list[dict]:
    return [r for r in _rules() if r.get("groupName") in KITS]


def test_each_kit_release_is_one_pr_across_its_depnames_must_fire() -> None:
    # a consumer, 2026-11-12: ci-kit v1.25.0 opened #2995 (pep621, depName `ci-kit`) AND #2996
    # (github-actions, depName `mjdevaccount/ci-kit`) -- two heavy unit runs for one release
    at, _ = _source_rule()
    groups = {r["groupName"]: (i, r) for i, r in enumerate(_rules()) if r in _kit_groups()}
    assert set(groups) == set(KITS)
    for kit, (i, r) in groups.items():
        assert i > at, f"{kit}: a later rule wins, so the group must follow the source-url rule"
        # the source URL both depName forms report; never a bare name (npm has unrelated packages under bare names)
        assert set(r["matchSourceUrls"]) == {f"https://github.com/mjdevaccount/{kit}{x}" for x in ("", ".git")}
        # ONE matcher: two matchers in one rule must both match
        assert set(r) - {"description"} == {"matchSourceUrls", *HOLDS}
        assert r["schedule"] == KIT_WINDOW and r["minimumReleaseAge"] is None
        # no later group re-splits it: one naming the kit, or one with no package filter on a kit's manager
        assert not [x for x in _rules()[i + 1:] if x not in _kit_groups() and x.get("groupName") is not None
                    and (kit in str(x) or (not any(k in x for k in NAME_MATCHERS)
                                           and (not x.get("matchManagers") or set(x["matchManagers"]) & KIT_MANAGERS)))]


# ── v1.35.0: one daily kit window, one weekly lock window, security at any time ───────────────
def _cfg() -> dict:
    return json5.loads(PRESET.read_text(encoding="utf-8"))


def window_faults(cfg: dict) -> list[str]:
    out = []
    kit = [r for r in cfg["packageRules"] if "schedule" in r and (
        "mjdevaccount" in str(r.get("matchPackageNames", "")) or "mjdevaccount" in str(r.get("matchSourceUrls", "")))]
    if len(kit) < 6 or any(r["schedule"] != KIT_WINDOW for r in kit):
        out.append("a first-party rule is off the one daily window")
    minute, hours, dom, month, dow = KIT_WINDOW[0].split()
    lo, hi = (int(h) for h in hours.split("-"))
    # one window every day, at most half of it, closed by noon ET: wide enough that a repo Mend
    # runs a few times a day still lands a run in it (MEASURED_RUNS_ET), narrow enough to fold a
    # day's releases into one PR -- "at any time" is the v1.35.0 burst this window exists to stop.
    if (minute, dom, month, dow) != ("*", "*", "*", "*") or not 1 <= hi - lo + 1 <= 12 or hi >= 12:
        out.append("the kit window is not one morning window every day")
    lock = cfg["lockFileMaintenance"]
    if lock.get("schedule") != ["* 0-5 * * 6"]:
        out.append("lock maintenance is not one weekly window")
    if "updateNotScheduled" in lock:  # global false strands a green lock PR where Renovate is the merger
        out.append("updateNotScheduled is set for every repo, not only the queue repos")
    held = [r for r in cfg["packageRules"] if r.get("matchUpdateTypes") == ["lockFileMaintenance"]
            and r.get("updateNotScheduled") is False]
    queue = next((r for r in cfg["packageRules"] if "automergeType" in r), {})
    if len(held) != 1 or set(held[0].get("matchRepositories") or ()) != set(queue.get("matchRepositories") or ("?",)):
        out.append("the queue repos' lock PR is re-pushed outside its window")
    vuln = cfg["vulnerabilityAlerts"]
    if vuln.get("schedule") != ["at any time"] or vuln.get("minimumReleaseAge") is not None or not vuln.get("enabled"):
        out.append("a security update waits for a window")
    return out


def test_the_shipped_windows_are_clean_must_not_fire() -> None:
    assert window_faults(_cfg()) == []


def test_each_window_fault_fires_must_fire() -> None:
    def mut(fn) -> list[str]:
        c = _cfg()
        fn(c)
        return window_faults(c)
    assert mut(lambda c: c["packageRules"][[r.get("groupName") for r in c["packageRules"]].index("ci-kit")].update(
        {"schedule": ["at any time"]}))
    assert mut(lambda c: c["lockFileMaintenance"].update({"schedule": ["* * * * 6", "* 0-17 * * 0"]}))
    assert mut(lambda c: c["lockFileMaintenance"].update({"updateNotScheduled": False}))

    def _held(c: dict) -> dict:
        return next(r for r in c["packageRules"] if r.get("matchUpdateTypes") == ["lockFileMaintenance"])
    assert mut(lambda c: _held(c).pop("updateNotScheduled"))
    assert mut(lambda c: _held(c).update({"matchRepositories": ["mjdevaccount/ci-kit"]}))
    assert mut(lambda c: _held(c).pop("matchRepositories"))
    assert mut(lambda c: c["vulnerabilityAlerts"].update({"schedule": ["* 0-5 * * 6"]}))
    assert mut(lambda c: c["vulnerabilityAlerts"].update({"minimumReleaseAge": "3 days"}))


def test_the_queue_repos_still_never_rebase_a_kit_branch() -> None:
    rules = [r for r in _cfg()["packageRules"] if r.get("matchRepositories") and r.get("rebaseWhen") == "never"]
    assert len(rules) == 1 and rules[0]["matchSourceUrls"] == ["https://github.com/mjdevaccount/**"]


# ── 2026-11-20: the window must contain a real cold repo's runs ──────────────────────────────────
# trade-infra's Mend runs, read from its Dependency Dashboard edit history (each edit is a run that
# changed the body, so these are a FLOOR on its runs), as ET hours per ET day. Under 04:00-07:59 not
# one of these days had a run in the window: ci-kit v1.42.0 sat "Awaiting Schedule" 11-16..11-19
# and ci-pin-drift was red 11-18, 11-19 and 11-20. A window that misses these misses that repo.
MEASURED_RUNS_ET = {
    "2026-11-16": (2, 8, 20),
    "2026-11-17": (2, 13, 22),
    "2026-11-18": (0, 8, 16),
    "2026-11-19": (0, 9),
}


def days_missed(window: list[str], runs: dict[str, tuple[int, ...]]) -> list[str]:
    lo, hi = (int(h) for h in window[0].split()[1].split("-"))
    return [day for day, hours in runs.items() if not any(lo <= h <= hi for h in hours)]


def test_the_kit_window_holds_a_cold_repos_measured_runs_must_not_fire() -> None:
    assert days_missed(KIT_WINDOW, MEASURED_RUNS_ET) == []


def test_the_old_four_hour_window_missed_them_must_fire() -> None:
    assert days_missed(["* 4-7 * * *"], MEASURED_RUNS_ET) == list(MEASURED_RUNS_ET)
