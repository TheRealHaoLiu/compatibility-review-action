"""Bare mirrors of the repositories the service reviews, one per repository under the data dir.

A mirror is a cache. Its size is bounded three ways: branches deleted upstream are pruned on every
fetch, a pull request's ref is dropped as soon as its review is stored so git's own garbage
collection reclaims the objects, and when all mirrors together exceed a budget the least recently
used ones are deleted and cloned again on their next review.
"""

import os
import shutil
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
    """Update every branch, drop branches deleted upstream, and bring in the PR head as refs/pull/<n>/head."""
    git(mirror, *_auth(auth_header), "fetch", "-q", "--prune", "origin",
        f"+refs/pull/{number}/head:refs/pull/{number}/head", "+refs/heads/*:refs/heads/*",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return f"refs/pull/{number}/head"


def release_pull(mirror, number):
    """Drop the PR head ref once its review is stored. The objects it alone kept reachable are
    pruned by the `git gc --auto` that later fetches run, after git's two-week grace period."""
    git(mirror, "update-ref", "-d", f"refs/pull/{number}/head")


def size(path):
    """Bytes under a directory."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def last_used(mirror):
    """When the mirror was last fetched (FETCH_HEAD), else created."""
    for name in ("FETCH_HEAD", "HEAD"):
        try:
            return (Path(mirror) / name).stat().st_mtime
        except OSError:
            continue
    return Path(mirror).stat().st_mtime


def is_mirror(data_dir, candidate):
    """True only for a bare repository directly under data_dir/mirrors: the one kind of directory
    this module ever deletes. A symlink or anything resolving elsewhere is not one."""
    root = (Path(data_dir) / "mirrors").resolve()
    p = Path(candidate)
    if p.is_symlink():
        return False
    p = p.resolve()
    return p.parent == root and p.name.endswith(".git") and (p / "HEAD").is_file() and (p / "objects").is_dir()


def enforce_budget(data_dir, limit_bytes, keep=None):
    """Delete the least recently used mirrors until the rest fit in limit_bytes.

    Returns (deleted paths, bytes remaining). `keep` is never deleted. A deleted mirror is cloned
    again on the next review of its repository.
    """
    root = Path(data_dir) / "mirrors"
    if not root.is_dir():
        return [], 0
    mirrors = [p for p in root.iterdir() if is_mirror(data_dir, p)]
    sizes = {p: size(p) for p in mirrors}
    total = sum(sizes.values())
    deleted = []
    for p in sorted(mirrors, key=last_used):
        if total <= limit_bytes:
            break
        if keep is not None and p.resolve() == Path(keep).resolve():
            continue
        if not is_mirror(data_dir, p):  # checked again right before the only recursive delete here
            continue
        shutil.rmtree(p)
        total -= sizes[p]
        deleted.append(p)
    return deleted, total
