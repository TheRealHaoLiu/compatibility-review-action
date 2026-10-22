"""Bare mirrors of the repositories the service reviews, one per repository under the data dir."""

import os
import subprocess
from pathlib import Path

from .change import git


def path(data_dir, owner, repo):
    return Path(data_dir) / "mirrors" / f"{owner}-{repo}.git"


def _auth(auth_header):
    return ["-c", f"http.extraheader={auth_header}"] if auth_header else []


def ensure(data_dir, owner, repo, auth_header=None, api_host="github.com"):
    """Clone the mirror on first use. Returns its path.

    A bare clone holds branches and tags only; pull request heads come in one at a time with
    `fetch_pull`. A `--mirror` clone would also fetch every `refs/pull/*` ref the remote advertises,
    thousands on an old repository, most of them merge commits nothing here reads.
    """
    dest = path(data_dir, owner, repo)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://{api_host}/{owner}/{repo}.git"
    p = subprocess.run(["git", *_auth(auth_header), "clone", "-q", "--bare", url, str(dest)],
                       capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if p.returncode:
        raise RuntimeError(f"git clone --mirror {owner}/{repo}: {p.stderr.strip()[:300]}")
    return dest


def fetch_pull(mirror, number, auth_header=None):
    """Update every branch and bring in the PR head as refs/pull/<n>/head."""
    git(mirror, *_auth(auth_header), "fetch", "-q", "origin", f"+refs/pull/{number}/head:refs/pull/{number}/head",
        "+refs/heads/*:refs/heads/*", env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return f"refs/pull/{number}/head"
