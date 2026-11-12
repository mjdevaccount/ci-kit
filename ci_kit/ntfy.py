"""ONE ntfy push (env `NTFY_URL`; unset = not pushed). The transport only: callers own the wording
and the dedup (pr-autoupdate's conflict label, for one). Moved out of pr_autoupdate (v1.27.0) so
the updater stays policy and any other ci-kit tool pages the same way."""
from __future__ import annotations

import os
import urllib.request


def page(title: str, body: str, click: str) -> str:
    """POST one notification; returns what happened, for the run summary. Never raises."""
    url = os.environ.get("NTFY_URL", "")
    if not url:
        return "NTFY_URL unset: NOT pushed"
    req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
        # priority 3: a PR is never a reason to wake the operator
        "Title": title, "Priority": "3", "Tags": "twisted_rightwards_arrows", "Click": click})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return f"published ({r.status})"
    except OSError as e:
        return f"push FAILED: {e}"
