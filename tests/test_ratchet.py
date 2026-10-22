"""ci_kit.ratchet -- the mechanics, pinned.

The migration itself was proven by byte-comparison (old-code regen vs new-code regen of every
baseline, plus identical check output on the current tree). These tests pin the MECHANICS so
the next linter can trust them without re-proving: load/skip-comments, regression detection,
shrink-is-fine, and the write round-trip in all three on-disk formats.
"""

from __future__ import annotations

import pathlib
from collections import Counter

from ci_kit import ratchet as _ratchet


def test_set_txt_round_trip_and_regressions(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "base.txt"
    r = _ratchet.RatchetSet(p, header="# hdr line 1\n# hdr line 2\n")
    r.write({"b|x", "a|y"})
    assert p.read_text(encoding="utf-8") == "# hdr line 1\n# hdr line 2\na|y\nb|x\n"
    assert r.load() == {"a|y", "b|x"}
    # new key = regression; removed key = silent (ratchet tightening)
    assert r.regressions({"a|y", "c|new"}) == ["c|new"]
    assert r.regressions({"a|y"}) == []


def test_set_missing_baseline_is_empty(tmp_path: pathlib.Path) -> None:
    r = _ratchet.RatchetSet(tmp_path / "nope.txt")
    assert r.load() == set()
    assert r.regressions({"anything"}) == ["anything"]


def test_set_json_round_trip(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "base.json"
    r = _ratchet.RatchetSet(p)
    r.write(["z.py", "a.py"])
    # exactly the json.dumps(sorted, indent=2)+"\n" contract the two json linters shipped
    assert p.read_text(encoding="utf-8") == '[\n  "a.py",\n  "z.py"\n]\n'
    assert r.load() == {"a.py", "z.py"}


def test_count_growth_fails_shrink_passes(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "counts.txt"
    r = _ratchet.RatchetCount(p, header="# hdr\n")
    r.write(Counter({"f.py|now()": 2, "g.py|current_date": 1}))
    assert p.read_text(encoding="utf-8") == "# hdr\nf.py|now()|2\ng.py|current_date|1\n"
    assert r.load() == {"f.py|now()": 2, "g.py|current_date": 1}
    # grown count and new key both flagged, with (key, current, baseline)
    regs = r.regressions(Counter({"f.py|now()": 3, "h.py|now()": 1, "g.py|current_date": 1}))
    assert regs == [("f.py|now()", 3, 2), ("h.py|now()", 1, 0)]
    # shrink is progress, not a regression
    assert r.regressions(Counter({"f.py|now()": 1})) == []


def test_count_key_may_contain_pipes(tmp_path: pathlib.Path) -> None:
    # keys are "file|token" — rpartition on the LAST pipe is what makes that safe
    p = tmp_path / "counts.txt"
    r = _ratchet.RatchetCount(p)
    r.write({"a|b|c": 4})
    assert r.load() == {"a|b|c": 4}


def test_comment_and_blank_lines_skipped(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "base.txt"
    p.write_text("# comment\n\nreal|key\n  # indented comment\n", encoding="utf-8")
    assert _ratchet.RatchetSet(p).load() == {"real|key"}


def test_count_shrunk_reports_improved_and_gone_keys(tmp_path):
    rc = _ratchet.RatchetCount(tmp_path / "b.txt", header="# h\n")
    rc.write({"a.py": 3, "b.py": 1})
    assert rc.shrunk({"a.py": 2}) == [("a.py", 2, 3), ("b.py", 0, 1)]
    assert rc.shrunk({"a.py": 3, "b.py": 1}) == []
    assert rc.shrunk({"a.py": 5, "b.py": 1}) == []
