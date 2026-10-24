"""Scheduled at 3 AM: wake the GitHub Actions night shift. Standard library only."""

import json
import os
import urllib.request


def main(args: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GH_REPO']}/dispatches",
        data=json.dumps({"event_type": "nap"}).encode(),
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "User-Agent": "mumble"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return {"body": {"woke": res.status == 204}}
