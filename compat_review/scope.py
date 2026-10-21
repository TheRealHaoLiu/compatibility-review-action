"""Per-repository settings: `.compatibility-review.toml` at the repository root.

    context = "A paragraph about the product and who installs it."   # or:
    context_file = "docs/compatibility-context.md"
    ignore = [".github/**", "**/tests/**", "docs/**"]   # a change touching only these paths is
                                                         # Green with no model call

Nothing is ignored unless the repository says so. Globs: `*` matches within one path segment,
`**` across segments, `?` one character; a pattern matches the whole path.
"""

import re
import tomllib
from pathlib import Path

CONFIG_NAME = ".compatibility-review.toml"
FILE = re.compile(r"^diff --git a/(\S+) b/", re.M)


def load(repo):
    f = Path(repo) / CONFIG_NAME
    return tomllib.loads(f.read_text()) if f.exists() else {}


def context_text(repo, cfg):
    """The product paragraph appended to the prompt: inline, from a file, or the generic default."""
    if cfg.get("context"):
        return cfg["context"].strip() + "\n"
    if cfg.get("context_file"):
        return (Path(repo) / cfg["context_file"]).read_text()
    return (Path(__file__).resolve().parent / "context.md").read_text()


def _regex(glob):
    out = ""
    i = 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out += r"(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += r".*"
            i += 2
        elif c == "*":
            out += r"[^/]*"
            i += 1
        elif c == "?":
            out += r"[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(f"^{out}$")


def ignored(path, globs):
    return any(_regex(g).match(path) for g in globs)


def is_test(path, globs=()):
    """Test code and ignored paths are left out of the repository facts."""
    name = Path(path).name
    return ("/tests/" in f"/{path}" or "/test/" in f"/{path}" or name.startswith("test_")
            or name.endswith("_test.py") or ignored(path, globs))


def paths_of(diff):
    return FILE.findall(diff)


def out_of_scope(diff, globs):
    """The changed paths if every one of them is ignored, else None."""
    paths = paths_of(diff)
    if paths and globs and all(ignored(p, globs) for p in paths):
        return paths
    return None


def skipped_result(paths):
    """A result for a change that never reached the model."""
    return {"verdict": "none", "color": "Green",
            "summary": f"Only ignored paths changed ({len(paths)} files); not reviewed.",
            "findings": [], "q_changes_existing_path": "no", "q_breaking_setup": "none",
            "q_mechanism": "none", "q_prevalence": "n/a", "q_auto_preserved": "n/a",
            "q_widens_access_or_changes_output": "no",
            "_meta": {"skipped": "out of scope", "paths": paths, "usage": {}}}
