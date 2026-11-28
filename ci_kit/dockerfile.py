"""A Dockerfile's COPY/ADD instructions as token lists. Library, stdlib only (v1.50.0).

MOVED FROM trade-data (`scripts/_dockerfile.py` `instructions()`), for `ci-image-pr-filter`. The
source repo keeps its own copy for a reader that runs on a runner's bare python3 (no venv, so no
ci-kit); its tests pin the two against each other, so the two answers to "what does this COPY line
mean" cannot drift apart unseen.
"""
from __future__ import annotations

import json
import shlex


def instructions(text: str) -> list[list[str]]:
    """Every COPY/ADD as its token list, flags stripped; continuations joined, comments dropped.

    A comment line inside a continuation is legal Dockerfile and does not end the instruction.
    The JSON form (`COPY ["a", "b"]`) is read with json; the shell form with shlex, so a quoted
    path with a space survives. A `--from=` instruction is dropped whole: its sources are not
    paths in this repo.
    """
    out: list[list[str]] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(chr(92)):
            buf += line[:-1] + " "
            continue
        buf += line
        head, _, rest = buf.partition(" ")
        buf = ""
        if head.upper() not in ("COPY", "ADD"):
            continue
        rest = rest.strip()
        tokens = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
        if any(t.startswith("--from=") for t in tokens):
            continue
        out.append([t for t in tokens if not t.startswith("--")])
    return out
