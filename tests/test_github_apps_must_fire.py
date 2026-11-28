"""ci-app-audit (ci_kit/github_apps.py): a declared GitHub App held to live. Bad = 1, good = 0,
could not look = 2, over a synthetic App on the fake GitHub the selftest uses
(ci_kit/github_apps_selftest.py: a real RS256 JWT, installation tokens, pagination)."""
from __future__ import annotations

import copy
import json
import pathlib

import pytest

from ci_kit import github_apps as ga
from ci_kit import github_apps_selftest as st


@pytest.fixture(scope="module")
def key() -> tuple[str, object]:
    return st.make_key()


def _world(key: tuple[str, object], **kw: int) -> st.FakeGitHub:
    return st.FakeGitHub(key[1], **kw)


def _m(**app: object) -> dict:
    m = copy.deepcopy(st.MANIFEST)
    m["apps"][0].update(app)
    return m


def test_the_selftest_passes() -> None:
    assert st.selftest() == []
    assert ga.main(["--selftest"]) == 0


def test_the_declared_app_is_clean(key: tuple[str, object]) -> None:
    w = _world(key)
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 0, out
    assert "apps=1 findings=0" in out and "2 installation(s), 2 selected repo(s)" in out
    assert w.scopes == [{"permissions": {"metadata": "read"}}] and w.revoked == list(w.tokens)


def test_no_key_jwt_or_token_reaches_the_output(key: tuple[str, object]) -> None:
    w = _world(key)
    w.app["permissions"]["actions"] = "write"
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 1 and key[0].splitlines()[1] not in out and not any(t in out for t in w.tokens)
    assert "eyJ" not in out  # no JWT


def _mut_widen(w: st.FakeGitHub) -> None:
    w.app["permissions"]["contents"] = "admin"


def _mut_add(w: st.FakeGitHub) -> None:
    w.app["permissions"]["administration"] = "write"


def _mut_narrow(w: st.FakeGitHub) -> None:
    w.app["permissions"]["contents"] = "read"


def _mut_drop(w: st.FakeGitHub) -> None:
    del w.app["permissions"]["pull_requests"]


def _mut_event_add(w: st.FakeGitHub) -> None:
    w.app["events"] = ["push"]


def _mut_repo_add(w: st.FakeGitHub) -> None:
    w.repos[11].append("repo-c")


def _mut_repo_drop(w: st.FakeGitHub) -> None:
    w.repos[11].remove("repo-b")


def _mut_install_add(w: st.FakeGitHub) -> None:
    w.installations.append({"id": 13, "account": {"login": "stranger"}, "repository_selection": "all",
                            "permissions": dict(st.PERMS), "events": [], "suspended_at": None})


def _mut_install_drop(w: st.FakeGitHub) -> None:
    w.installations.pop(1)


def _mut_suspend(w: st.FakeGitHub) -> None:
    w.installations[1]["suspended_at"] = "2026-01-01T00:00:00Z"


def _mut_selection(w: st.FakeGitHub) -> None:
    w.installations[1]["repository_selection"] = "selected"
    w.repos[12] = ["repo-a"]


def _mut_install_lags(w: st.FakeGitHub) -> None:
    w.installations[0]["permissions"]["pull_requests"] = "read"  # the App changed, the owner did not accept


def _mut_install_events(w: st.FakeGitHub) -> None:
    w.installations[0]["events"] = ["pull_request"]


def _mut_app_id(w: st.FakeGitHub) -> None:
    w.app["id"] = 999


@pytest.mark.parametrize("mutate,want", [
    (_mut_widen, "WIDENED contents: declared write, live admin"),
    (_mut_add, "WIDENED administration: declared none, live write"),
    (_mut_narrow, "narrowed contents: declared write, live read"),
    (_mut_drop, "narrowed pull_requests: declared write, live none"),
    (_mut_event_add, "event ADDED push"),
    (_mut_repo_add, "repository INSTALLED, not declared: repo-c"),
    (_mut_repo_drop, "repository declared, not installed: repo-b"),
    (_mut_install_add, "on stranger: installation NOT DECLARED"),
    (_mut_install_drop, "on example-user: declared installation MISSING"),
    (_mut_suspend, "installation suspended"),
    (_mut_selection, "repository_selection selected, declared all"),
    (_mut_install_lags, "(installation): permission narrowed pull_requests"),
    (_mut_install_events, "installation events ['pull_request']"),
    (_mut_app_id, "live app_id 999"),
], ids=lambda x: getattr(x, "__name__", "")[5:] if callable(x) else "")
def test_live_drift_is_1_must_fire(key: tuple[str, object], mutate, want: str) -> None:
    w = _world(key)
    mutate(w)
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 1 and want in out, out


@pytest.mark.parametrize("app,want", [
    ({"default_events": ["push"]}, "event dropped push"),
    ({"default_permissions": {"contents": "write"}}, "WIDENED pull_requests"),
])
def test_declaration_drift_is_1_must_fire(key: tuple[str, object], app: dict, want: str) -> None:
    rc, out = st.run(_m(**app), _world(key), key[0])
    assert rc == 1 and want in out, out


def test_metadata_read_is_implicit_and_may_be_declared(key: tuple[str, object]) -> None:
    perms = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    assert st.run(_m(default_permissions=perms), _world(key), key[0])[0] == 0


def test_a_per_installation_declaration_is_what_that_installation_is_held_to(key: tuple[str, object]) -> None:
    w = _world(key)
    _mut_install_lags(w)
    m = _m()
    m["apps"][0]["installations"][0]["permissions"] = {"contents": "write", "pull_requests": "read"}
    assert st.run(m, w, key[0])[0] == 0


def test_repositories_past_one_page_are_all_read(key: tuple[str, object]) -> None:
    w = _world(key, repos_a=250)
    m = _m()
    m["apps"][0]["installations"][0]["repositories"] = w.repos[11][:]
    rc, out = st.run(m, w, key[0])
    assert rc == 0 and "250 selected repo(s)" in out
    m["apps"][0]["installations"][0]["repositories"] = w.repos[11][:-1]  # the 250th, on page 3
    rc, out = st.run(m, w, key[0])
    assert rc == 1 and "INSTALLED, not declared: bulk-247" in out


def test_installations_past_one_page_are_all_read(key: tuple[str, object]) -> None:
    w = _world(key)
    for i in range(150):
        w.installations.append({"id": 100 + i, "account": {"login": f"acct-{i}"}, "repository_selection": "all",
                                "permissions": dict(st.PERMS), "events": [], "suspended_at": None})
    m = _m()
    m["apps"][0]["installations"] += [{"account": f"acct-{i}", "repository_selection": "all"} for i in range(149)]
    rc, out = st.run(m, w, key[0])
    assert rc == 1 and "on acct-149: installation NOT DECLARED" in out


BAD_MANIFESTS = [
    "{not json", json.dumps({"apps": []}), json.dumps({"app": [{}]}),
    json.dumps({"apps": [{"client_id": "x", "installations": []}]}),                  # no name
    json.dumps({"apps": [{"name": "a", "installations": []}]}),                       # no issuer
    json.dumps({"apps": [{"name": "a", "app_id": "1", "installations": []}]}),        # app_id not int
    json.dumps({"apps": [{"name": "a", "app_id": 1}]}),                               # no installations
    json.dumps({"apps": [{"name": "a", "app_id": 1, "installations": [], "permisions": {}}]}),  # typo key
    json.dumps({"apps": [{"name": "a", "app_id": 1, "installations": [], "default_permissions": {"x": "rw"}}]}),
    json.dumps({"apps": [{"name": "a", "app_id": 1, "installations": [{"account": "o"}]}]}),
    json.dumps({"apps": [{"name": "a", "app_id": 1, "installations": [
        {"account": "o", "repository_selection": "all", "repositories": ["r"]}]}]}),
    json.dumps({"apps": [{"name": "a", "app_id": 1, "installations": [
        {"account": "o", "repository_selection": "selected"}]}]}),
    json.dumps({"apps": [{"name": "a", "app_id": 1, "installations": [
        {"account": "o", "repository_selection": "all"}, {"account": "O", "repository_selection": "all"}]}]}),
    json.dumps({"apps": [{"name": "a", "app_id": 1, "installations": []}, {"name": "a", "app_id": 2, "installations": []}]}),
]


@pytest.mark.parametrize("bad", BAD_MANIFESTS)
def test_an_unreadable_manifest_is_2_must_fire(key: tuple[str, object], bad: str) -> None:
    rc, out = st.run(bad, _world(key), key[0])
    assert rc == 2 and "COULD NOT LOOK" in out, out


def test_github_manifest_keys_are_accepted(key: tuple[str, object]) -> None:
    m = _m(url="https://example.com", public=False, hook_attributes={"active": False}, description="d")
    assert st.run(m, _world(key), key[0])[0] == 0


def test_a_key_github_refuses_is_2_must_fire(key: tuple[str, object]) -> None:
    other, _ = st.make_key()
    rc, out = st.run(st.MANIFEST, _world(key), other)
    assert rc == 2 and "HTTP 401" in out


def test_a_key_that_is_not_a_key_is_2_and_not_echoed(key: tuple[str, object]) -> None:
    rc, out = st.run(st.MANIFEST, _world(key), "-----BEGIN PRIVATE KEY-----\nnot-a-key-SENTINEL\n-----END PRIVATE KEY-----\n")
    assert rc == 2 and "could not sign" in out and "SENTINEL" not in out


def test_another_apps_key_is_2_never_its_drift(key: tuple[str, object]) -> None:
    w = _world(key)
    w.app["slug"] = "other-bot"
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 2 and "is the key of App other-bot" in out


@pytest.mark.parametrize("where", ["/app/installations", "/app/installations/11", "/installation/repositories", "/app"])
def test_any_failed_read_is_2_must_fire(key: tuple[str, object], where: str) -> None:
    w = _world(key)
    w.fail = where
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 2 and "HTTP 500" in out


def test_a_failed_revoke_does_not_blind_the_read_and_is_said(key: tuple[str, object]) -> None:
    w = _world(key)
    w.fail = "/installation/token"
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 0 and "scoped read token was NOT revoked" in out, out


def test_one_unread_app_never_hides_another_apps_drift_must_fire(key: tuple[str, object],
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyed App that drifted + an app-level App GitHub will not show: exit 2, and the first
    App's drift is still printed (the reviewer's probe: it was thrown away)."""
    monkeypatch.setenv("GH_TOKEN", "t")
    m = copy.deepcopy(st.MANIFEST)
    m["apps"].append({"name": "other-bot", "app_id": 7, "default_permissions": {}, "installations": []})
    w = _world(key)
    _mut_add(w)
    rc, out = st.run(m, w, key[0], "--app-level", "other-bot")
    assert rc == 2, out
    assert "WIDENED administration" in out and "COULD NOT LOOK: App other-bot" in out and "unread=1" in out


def test_a_crash_reading_an_app_is_2_never_drift_must_fire(key: tuple[str, object]) -> None:
    w = _world(key)
    w.installations[0]["account"] = "not-a-mapping"
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 2 and "CRASHED AttributeError at github_apps.py:" in out, out
    assert "not-a-mapping" not in out  # a crash names its type and place, never a value


def test_a_broken_http_response_is_blind_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    def broken(*a: object, **k: object) -> object:
        raise http.client.IncompleteRead(b"")
    monkeypatch.setattr(ga.urllib.request, "urlopen", broken)
    with pytest.raises(ga.Blind, match="IncompleteRead"):
        ga.urllib_transport("GET", "/app", "x")


def test_the_reusable_never_globs_its_name_lists() -> None:
    text = (pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "app-audit.yml").read_text(encoding="utf-8")
    assert "set -f" in text and text.index("set -f") < text.index("for n in $APP_LEVEL")


def test_no_key_without_app_level_is_2_must_fire(key: tuple[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    rc, out = st.run(st.MANIFEST, _world(key), None)
    assert rc == 2 and "--app-level example-bot" in out


def test_app_level_reads_permissions_and_names_what_it_did_not_look_at(key: tuple[str, object],
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    w = _world(key)
    rc, out = st.run(st.MANIFEST, w, None, "--app-level", st.SLUG)
    assert rc == 0 and "APP LEVEL ONLY" in out and "2 declared installation(s) NOT looked at" in out
    assert w.calls == [f"GET /apps/{st.SLUG}"]
    _mut_add(w)
    rc, out = st.run(st.MANIFEST, w, None, "--app-level", st.SLUG)
    assert rc == 1 and "WIDENED administration" in out


def test_app_level_without_a_token_is_2(key: tuple[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    assert st.run(st.MANIFEST, _world(key), None, "--app-level", st.SLUG)[0] == 2


def test_the_key_from_an_env_var(key: tuple[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_KEY", key[0])
    assert st.run(st.MANIFEST, _world(key), None, "--key-env", f"{st.SLUG}=APP_KEY")[0] == 0
    monkeypatch.setenv("APP_KEY", "  ")
    assert st.run(st.MANIFEST, _world(key), None, "--key-env", f"{st.SLUG}=APP_KEY")[0] == 2


@pytest.mark.parametrize("args", [["--only", "nope"], ["--app-level", "nope"], ["--key-env", "nope=X"],
                                  ["--key-env", "noequals"]])
def test_flags_naming_no_declared_app_are_2(key: tuple[str, object], args: list[str]) -> None:
    assert st.run(st.MANIFEST, _world(key), key[0], *args)[0] == 2


def test_a_missing_key_file_is_2(key: tuple[str, object], tmp_path: pathlib.Path) -> None:
    m = tmp_path / "m.json"
    m.write_text(json.dumps(st.MANIFEST), encoding="utf-8")
    assert ga.main(["--manifest", str(m), "--key-file", f"{st.SLUG}={tmp_path / 'nope.pem'}"], call=_world(key)) == 2
    assert ga.main([], call=_world(key)) == 2  # no manifest at all


def test_a_yaml_manifest(key: tuple[str, object], tmp_path: pathlib.Path) -> None:
    yaml = pytest.importorskip("yaml")
    m, k = tmp_path / "apps.yml", tmp_path / "k.pem"
    m.write_text(yaml.safe_dump(st.MANIFEST), encoding="utf-8")
    k.write_text(key[0], encoding="utf-8")
    assert ga.main(["--manifest", str(m), "--key-file", f"{st.SLUG}={k}"], call=_world(key)) == 0
    m.write_text("apps: [unclosed", encoding="utf-8")
    assert ga.main(["--manifest", str(m), "--key-file", f"{st.SLUG}={k}"], call=_world(key)) == 2


def test_in_actions_drift_is_an_error_annotation_and_a_summary(key: tuple[str, object], tmp_path: pathlib.Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    w = _world(key)
    _mut_add(w)
    rc, out = st.run(st.MANIFEST, w, key[0])
    assert rc == 1 and "::error title=ci-app-audit::App example-bot: permission WIDENED administration" in out
    assert "1 drift finding(s)" in summary.read_text(encoding="utf-8")


def test_perm_drift_orders_levels() -> None:
    assert ga.perm_drift({"a": "read"}, {"a": "write"}, "x") == ["x: permission WIDENED a: declared read, live write"]
    assert ga.perm_drift({"a": "admin"}, {"a": "write"}, "x") == ["x: permission narrowed a: declared admin, live write"]
    assert ga.perm_drift({"metadata": "read"}, {}, "x") == []  # implicit on both sides


# ── --validate: the manifest's shape, keyless, no network (the pre-merge check) ──────────────────
def test_validate_a_good_manifest_is_0_and_calls_nothing(key: tuple[str, object], tmp_path: pathlib.Path) -> None:
    w = _world(key)
    m = tmp_path / "m.json"
    m.write_text(json.dumps(st.WITH_OTHER), encoding="utf-8")
    rc = ga.main(["--manifest", str(m), "--validate", "--key-file", f"{st.SLUG}={tmp_path / 'absent.pem'}",
                  "--app-level", st.OTHER, "--app-level-token-from", st.SLUG], call=w)
    assert rc == 0 and w.calls == []  # the key file does not exist: it was never read


@pytest.mark.parametrize("bad", BAD_MANIFESTS)
def test_validate_an_invalid_manifest_is_1_must_fire(key: tuple[str, object], bad: str) -> None:
    w = _world(key)
    rc, out = st.run(bad, w, None, "--validate")
    assert rc == 1 and "INVALID" in out and w.calls == [], out


def test_validate_an_unreadable_manifest_is_2_must_fire(tmp_path: pathlib.Path) -> None:
    assert ga.main(["--manifest", str(tmp_path / "absent.yml"), "--validate"], call=None) == 2
    m = tmp_path / "m.yml"
    m.write_bytes(b"\xff\xfe not utf-8")
    assert ga.main(["--manifest", str(m), "--validate"], call=None) == 2


@pytest.mark.parametrize("args", [["--only", "nope"], ["--app-level", "nope"], ["--app-level-token-from", "nope"]])
def test_validate_flags_naming_no_declared_app_are_2(key: tuple[str, object], args: list[str]) -> None:
    assert st.run(st.MANIFEST, _world(key), None, "--validate", *args)[0] == 2


def test_validate_in_actions_is_an_error_annotation(key: tuple[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc, out = st.run(_m(permisions={}), _world(key), None, "--validate")
    assert rc == 1 and "::error title=ci-app-audit::apps[0] example-bot: unknown key(s) permisions" in out


# ── --app-level-token-from: a PRIVATE App read with the keyed App's scoped, revoked token ────────
def _private(key: tuple[str, object]) -> st.FakeGitHub:
    w = _world(key)
    w.private[st.OTHER] = copy.deepcopy(st.OTHER_APP)
    return w


def test_a_private_app_is_hidden_from_a_plain_token_must_fire(key: tuple[str, object],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """The live failure this flag fixes: GITHUB_TOKEN gets 403 on a private App -> exit 2."""
    monkeypatch.setenv("GH_TOKEN", "a-workflow-token")
    rc, out = st.run(st.WITH_OTHER, _private(key), key[0], "--app-level", st.OTHER)
    assert rc == 2 and f"GET /apps/{st.OTHER}: HTTP 403" in out


def test_the_reader_token_reads_a_private_app_scoped_and_revoked(key: tuple[str, object]) -> None:
    w = _private(key)
    rc, out = st.run(st.WITH_OTHER, w, key[0], "--app-level", st.OTHER, "--app-level-token-from", st.SLUG)
    assert rc == 0, out
    assert all(x == {"permissions": {"metadata": "read"}} for x in w.scopes) and len(w.scopes) == 2
    assert sorted(w.revoked) == sorted(w.tokens)
    assert "installation token of example-bot (installation 11), revoked" in out and "APP LEVEL ONLY" in out
    assert not any(t in out for t in w.tokens)


def test_a_private_apps_drift_read_by_the_reader_is_1_must_fire(key: tuple[str, object]) -> None:
    w = _private(key)
    w.private[st.OTHER]["permissions"]["administration"] = "write"
    rc, out = st.run(st.WITH_OTHER, w, key[0], "--app-level", st.OTHER, "--app-level-token-from", st.SLUG)
    assert rc == 1 and "App other-bot: permission WIDENED administration" in out


def test_a_failed_reader_mint_blinds_the_app_level_app_only_must_fire(key: tuple[str, object]) -> None:
    w = _private(key)
    _mut_add(w)  # the keyed App drifted: still said
    w.installations[0]["suspended_at"] = w.installations[1]["suspended_at"] = "2026-01-01T00:00:00Z"
    rc, out = st.run(st.WITH_OTHER, w, key[0], "--app-level", st.OTHER, "--app-level-token-from", st.SLUG)
    assert rc == 2, out
    assert "WIDENED administration" in out and "no app-level reader token from example-bot" in out
    assert "no unsuspended installation" in out and "unread=1" in out


def test_a_failed_reader_revoke_is_said_not_blind(key: tuple[str, object]) -> None:
    w = _private(key)
    w.fail = "/installation/token"
    rc, out = st.run(st.WITH_OTHER, w, key[0], "--app-level", st.OTHER, "--app-level-token-from", st.SLUG)
    assert rc == 0 and "app-level reader token (example-bot, installation 11, metadata: read) was NOT revoked" in out


def test_no_app_level_app_mints_no_reader(key: tuple[str, object]) -> None:
    w = _world(key)
    rc, out = st.run(st.MANIFEST, w, key[0], "--app-level-token-from", st.SLUG)
    assert rc == 0 and len(w.scopes) == 1 and "reader" not in out  # the selected installation's read only


@pytest.mark.parametrize("args", [
    ["--app-level-token-from", st.OTHER],                                   # declared, but no key here
    ["--app-level-token-from", st.SLUG, "--token-env", "GH_TOKEN"],         # two app-level tokens
])
def test_a_reader_that_cannot_be_is_2(key: tuple[str, object], args: list[str]) -> None:
    rc, out = st.run(st.WITH_OTHER, _private(key), key[0], "--app-level", st.OTHER, *args)
    assert rc == 2, out


def test_the_reusable_passes_the_reader_flag_only_for_its_keyed_app() -> None:
    text = (pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "app-audit.yml").read_text(encoding="utf-8")
    assert "app-level-reader:" in text and '--app-level-token-from $KEYED' in text
    assert '[ "$READER" = true ]' in text
