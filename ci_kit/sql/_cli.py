"""What the five SQL gates' CLIs share on top of `ci_kit.site_lint.check`.

Each gate parses its flags with argparse (value flags, `--help`, a refused unknown flag), then hands
the RUN to `site_lint.check`: `--selftest`, the gate's modes, the scan, and exit 0 clean / 1
findings / 2 could not look -- including a scan that crashes, which used to be a traceback and so
exit 1, the findings code. The gate keeps its voice through check's `report` hooks.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from collections.abc import Callable


def flags(args: argparse.Namespace, *names: str) -> list[str]:
    """The argv `site_lint.check` reads by membership, rebuilt from the PARSED args, so a flag
    argparse accepted under an abbreviation (`--write` for `--write-baseline`) still selects its
    mode instead of falling through to a check."""
    return ["--" + n.replace("_", "-") for n in names if getattr(args, n, False)]


def selftest_report(verdict: str, *, stream: str = "stderr") -> Callable[[list[str]], str]:
    """The selftest voice these gates shipped: each failure as `  SELFTEST FAIL: ...` on `stream`,
    then `<verdict> OK|FAILED` on stdout."""
    def report(fails: list[str]) -> str:
        out = sys.stderr if stream == "stderr" else sys.stdout
        for f in fails:
            print(f"  SELFTEST FAIL: {f}", file=out)
        return f"{verdict} {'FAILED' if fails else 'OK'}"
    return report


def blind_probe(probe: Callable[[], object], *, missed: str, wrong: str) -> str | None:
    """Run a selftest's blind case -- input the gate must refuse with exit 2 -- and return the
    failure string, or None when it exited 2. The probe's own `DID NOT FINISH LOOKING` line is
    CAPTURED, not printed: it is the mechanism working, and on a passing selftest it would read
    as a gate that went blind to anyone (or anything) scanning the job log for that phrase.
    `wrong` takes `{code}`."""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            probe()
        except SystemExit as exc:
            return None if exc.code == 2 else wrong.format(code=exc.code)
    return missed
