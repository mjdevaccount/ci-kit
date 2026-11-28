"""ci-migration-open-prs' request path: what GitHub answers -> what the gate counts (moved from the
source repo's httpx MockTransport tests onto the kit's injected `call`)."""
from __future__ import annotations

import re
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from ci_kit import migration_open_prs as gate

MIG = re.compile(r"^migrations/(\d{3,})_[^/]+\.sql$")


def _node(num, files, *, draft=False, more=False):
    return {"number": num, "headRefName": f"b{num}", "isDraft": draft,
            "files": {"pageInfo": {"hasNextPage": more}, "nodes": files}}


def _github(*nodes, errors=None):
    def call(method, url, body=None):
        if url.endswith("/graphql"):
            assert method == "POST" and body and "query" in body
            if errors:
                return {"errors": errors}
            page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(nodes)}
            return {"data": {"repository": {"pullRequests": page}}}
        page = int(parse_qs(urlparse(url).query)["page"][0])
        if page == 1:
            return [{"filename": f"src/f{i}.py", "status": "added"} for i in range(100)]
        return [{"filename": "migrations/390_big.sql", "status": "renamed"},
                {"filename": "migrations/391_gone.sql", "status": "removed"}]
    return call


def test_added_renamed_and_copied_count_drafts_and_other_changes_do_not() -> None:
    prs = gate.open_prs("o/r", _github(
        _node(1, [{"path": "migrations/387_a.sql", "changeType": "ADDED"},
                  {"path": "migrations/388_b.sql", "changeType": "RENAMED"},
                  {"path": "migrations/389_c.sql", "changeType": "COPIED"},
                  {"path": "migrations/380_d.sql", "changeType": "MODIFIED"},
                  {"path": "migrations/381_e.sql", "changeType": "DELETED"}]),
        _node(2, [{"path": "migrations/392_f.sql", "changeType": "ADDED"}], draft=True)))
    assert [p["number"] for p in prs] == [1]
    assert sorted(gate.numbers(prs[0]["added"], MIG)) == ["387", "388", "389"]


def test_a_pr_past_100_files_is_reread_over_rest_not_judged_truncated() -> None:
    prs = gate.open_prs("o/r", _github(_node(7, [], more=True)))
    assert gate.numbers(prs[0]["added"], MIG) == {"390": "migrations/390_big.sql"}


def test_a_graphql_error_payload_is_blind_not_an_empty_list() -> None:
    with pytest.raises(gate.Blind):
        gate.open_prs("o/r", _github(errors=[{"message": "bad credentials"}]))


def test_an_http_failure_is_blind(monkeypatch) -> None:
    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 502, "bad gateway", {}, None)
    monkeypatch.setattr(gate.urllib.request, "urlopen", refuse)
    with pytest.raises(gate.Blind):
        gate.open_prs("o/r", gate.http("t"))


def test_own_pr_number_wins_over_a_shared_head_name() -> None:
    # A fork PR can share the head NAME of an older PR; the number decides, not the name.
    out = gate.findings({"387": "migrations/387_mine.sql"}, {},
                        [{"number": 2852, "head": "feature/x", "added": ["migrations/387_theirs.sql"]}],
                        2856, "feature/x", MIG)
    assert len(out) == 1 and "#2852" in out[0]
