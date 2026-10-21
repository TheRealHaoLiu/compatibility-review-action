"""One change in a git repository: its diff from the merge base, the tree before it, the input."""

import subprocess
import tarfile
from pathlib import Path

MAX_DIFF_BYTES = 150_000


def git(repo, *args, env=None):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout


def extract_tree(repo, ref, dest):
    """`git archive ref` into dest. Works on bare mirrors; nothing touches a working checkout."""
    archive = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", ref], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
            tar.extractall(dest, filter="data")
    finally:
        archive.stdout.close()
    if archive.wait():
        raise RuntimeError(f"git archive {ref} failed")


def diff_from_merge_base(repo, base, head):
    """(merge_base, head_sha, diff) for the change from base to head."""
    merge_base = git(repo, "merge-base", base, head).strip()
    head_sha = git(repo, "rev-parse", head).strip()
    return merge_base, head_sha, git(repo, "diff", merge_base, head_sha)


def build_input(branch, title, body, diff, facts=None):
    """The reviewer's input: target branch, title, author description, diff, then tooling facts."""
    if len(diff) > MAX_DIFF_BYTES:
        diff = diff[:MAX_DIFF_BYTES] + f"\n[... diff truncated at {MAX_DIFF_BYTES} bytes of {len(diff)} ...]"
    parts = ["# Pull request", "", f"Target branch: {branch}", f"Title: {title}", "",
             "## Description (written by the author)", "", body or "", "", "## Diff", "",
             "```diff", diff.rstrip("\n"), "```"]
    if facts:
        parts += ["", "## Repository facts (computed by tooling, not written by the author)", "", facts]
    return "\n".join(parts) + "\n"


def read_config(repo, ref):
    """`.compatibility-review.toml` at `ref`, or '' when the repository has none."""
    try:
        return git(repo, "show", f"{ref}:.compatibility-review.toml")
    except RuntimeError:
        return ""


def context_file(repo, ref, path):
    return git(repo, "show", f"{ref}:{path}")
