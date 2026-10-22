"""The ratchet mechanism, defined once.

One copy instead of one per repo. Consumers pin a release TAG, and `ci-pin-drift` says when that
pin falls behind.

Seven linters in one service repo each re-implemented the same three moves — load a frozen
baseline, diff current findings against it, rewrite the baseline on --write-baseline — in
three drifted formats (five .txt set-of-keys, two .json sorted-lists, one .txt key|count).
Zero imports between them; two had no load function at all. This module is those three moves,
once, in both modes:

  RatchetSet    a frozen SET of keys. A key not in the baseline is a regression.
                Shrinking is silently fine (that is the ratchet tightening).
  RatchetCount  a frozen COUNT per key. A new key, or a grown count, is a regression.
                A shrunk count is fine and --write-baseline captures the tighter number.

What deliberately stays IN each linter: the scan itself, the header prose of its baseline
file, its failure message, its exit codes, its output formatting. The harness owns mechanics,
not voice — that is what made migrating seven linters a byte-identical-output change.

Baseline file formats are preserved exactly as each linter shipped them:
  .txt  set    header comment lines + one key per line, sorted
  .txt  count  header comment lines + "key|count" per line, sorted
  .json set    json.dumps(sorted(keys), indent=2) + newline   (no header support — json)

House rule: when a ratchet fires, fix the code. Re-blessing the
baseline is the exception and needs a stated reason in the commit.
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from collections.abc import Iterable


class RatchetSet:
    """A frozen set of grandfathered keys; anything new fails."""

    def __init__(self, path: pathlib.Path, header: str = "") -> None:
        self.path = path
        self.header = header
        self._json = path.suffix == ".json"

    def load(self) -> set[str]:
        if not self.path.exists():
            return set()
        if self._json:
            return set(json.loads(self.path.read_text(encoding="utf-8")))
        return {
            ln.strip()
            for ln in self.path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        }

    def write(self, keys: Iterable[str]) -> int:
        # newline="\n" EXPLICITLY. write_text() applies os.linesep translation, so a --write run
        # on Windows rewrites every baseline with CRLF. A repo without .gitattributes has
        # nothing but the local core.autocrlf setting stands between that and a committed
        # whole-file diff on a one-line change — and on a checkout that came back CRLF, every
        # ratchet would read as a hundred regressions at once. Sixth time this codebase has been
        # bitten by pathlib's platform-dependent default; the fix belongs at the write, not in
        # remembering.
        ks = sorted(set(keys))
        if self._json:
            self.path.write_text(json.dumps(ks, indent=2) + "\n", encoding="utf-8", newline="\n")
        else:
            self.path.write_text(self.header + "\n".join(ks) + "\n",
                                 encoding="utf-8", newline="\n")
        return len(ks)

    def regressions(self, current: Iterable[str]) -> list[str]:
        """Keys present now but not grandfathered — sorted, ready to print."""
        return sorted(set(current) - self.load())


class RatchetCount:
    """A frozen per-key count; a new key or a grown count fails, shrinking is progress."""

    def __init__(self, path: pathlib.Path, header: str = "") -> None:
        self.path = path
        self.header = header

    def load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        out: dict[str, int] = {}
        for ln in self.path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            key, _, n = ln.rpartition("|")
            if key and n.isdigit():
                out[key] = int(n)
        return out

    def write(self, counts: Counter[str] | dict[str, int]) -> int:
        body = "\n".join(f"{k}|{v}" for k, v in sorted(counts.items()))
        self.path.write_text(self.header + body + "\n", encoding="utf-8", newline="\n")  # see above
        return len(counts)

    def regressions(self, current: Counter[str] | dict[str, int]) -> list[tuple[str, int, int]]:
        """(key, current_count, baseline_count) for every new-or-grown key; baseline 0 = new."""
        base = self.load()
        out: list[tuple[str, int, int]] = []
        for key, n in sorted(current.items()):
            b = base.get(key, 0)
            if n > b:
                out.append((key, n, b))
        return out

    def shrunk(self, current: Counter[str] | dict[str, int]) -> list[tuple[str, int, int]]:
        """(key, current_count, baseline_count) for every improved-or-gone key — the linter's
        cue to tell the author the baseline can tighten (never a failure; ratchets only bite
        on growth)."""
        base = self.load()
        out: list[tuple[str, int, int]] = []
        for key, b in sorted(base.items()):
            n = int(current.get(key, 0))
            if n < b:
                out.append((key, n, b))
        return out
