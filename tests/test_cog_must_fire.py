"""ci-cog must fire: a stale generated fact -> 1, a current one -> 0, no tracked file carrying a block
or a generator that RAISES -> 2; --write regenerates."""
from __future__ import annotations

import pytest

pytest.importorskip("cogapp")

from _pylint_repo import again, run  # noqa: E402
from conftest import write  # noqa: E402

MOD = "cog_gate"
GOOD = '# Facts\n\nThe roster has <!-- [[[cog cog.outl(str(len(["a", "b", "c"]))) ]]] -->\n3\nentries. <!-- [[[end]]] -->\n'
STALE = GOOD.replace("\n3\n", "\n2\n")


def test_a_current_block_passes(new_repo) -> None:
    _, r = run(new_repo, MOD, {"docs/facts.md": GOOD})
    assert r.returncode == 0 and "cog: OK -- 1 file(s): docs/facts.md" in r.stdout, r.stdout + r.stderr


def test_a_stale_block_fires_and_write_regenerates(new_repo) -> None:
    repo, r = run(new_repo, MOD, {"docs/facts.md": STALE}, "--rerun", "make cog")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STALE" in r.stderr and "run `make cog`" in r.stderr and "+3" in r.stdout
    assert again(MOD, repo, "--write").returncode == 0
    assert (repo / "docs/facts.md").read_text(encoding="utf-8") == GOOD
    assert again(MOD, repo).returncode == 0


def test_no_block_anywhere_is_2(new_repo) -> None:
    _, r = run(new_repo, MOD, {"README.md": "# Facts\n\nA hand-written count: 3.\n"})
    assert r.returncode == 2 and "BLIND -- no tracked .md file carries a cog block" in r.stderr, r.stdout + r.stderr


def test_an_untracked_doc_and_a_contained_suffix_are_not_cogged(new_repo) -> None:
    repo, _ = run(new_repo, MOD, {"docs/facts.md": GOOD, "docs/x.md.fixture": STALE})
    write(repo / "docs/scratch.md", STALE)
    r = again(MOD, repo)
    assert r.returncode == 0 and "1 file(s)" in r.stdout, r.stdout + r.stderr


def test_a_raising_generator_is_2_without_a_traceback_header(new_repo) -> None:
    raises = GOOD.replace('cog.outl(str(len(["a", "b", "c"])))', 'raise ValueError("prose drifted")')
    _, r = run(new_repo, MOD, {"docs/facts.md": raises})
    assert r.returncode == 2 and "prose drifted" in r.stdout + r.stderr and "cog exited 4" in r.stderr
    assert "Traceback (most recent call last)" not in r.stdout + r.stderr


def test_a_generator_imports_from_include(new_repo) -> None:
    doc = "<!-- [[[cog import facts; cog.outl(facts.N) ]]] -->\n7\n<!-- [[[end]]] -->\n"
    _, r = run(new_repo, MOD, {"docs/a.md": doc, "tools/facts.py": 'N = "7"\n'}, "--include", "tools")
    assert r.returncode == 0, r.stdout + r.stderr
