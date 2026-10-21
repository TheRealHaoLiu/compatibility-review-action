"""Repository facts computed by tooling from the code before the change. No model involved.

    facts(diff, base_tree, is_test) -> markdown

  1. The full definition (function, or class header and nearby lines) around each change.
  2. One hop of references: call sites of changed functions, subclasses of changed classes.

Python only for now: definitions come from the AST, references from a grep over *.py.
"""

import ast
import re
import subprocess
from pathlib import Path

MAX_FACT_CHARS = 40_000
MAX_DEF_LINES = 120
MAX_HITS = 8
GENERIC = {"get", "set", "save", "delete", "update", "create", "run", "list", "post", "put", "patch", "clean",
           "validate", "start", "cancel", "send", "copy", "check", "main", "handle", "setUp", "tearDown", "Meta"}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def hunks(diff):
    """Yield (path, old_start, old_len, old_lines, changed) per hunk of a unified diff.

    `changed` holds the old line numbers the hunk touches: removed lines, and for added lines
    the old line they are inserted before. Context lines are left out, so the enclosing
    definition is looked up around the edit, not around the hunk's context."""
    path, cur, old_no = None, None, 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if cur:
                yield cur
            path, cur = None, None
        elif line.startswith("--- "):
            path = None if line[4:] == "/dev/null" else line[6:]
        elif (m := HUNK.match(line)):
            if cur:
                yield cur
            old_no = int(m[1])
            cur = (path, old_no, int(m[2] or 1), [], []) if path else None
        elif cur and line[:1] == " ":
            cur[3].append(line[1:])
            old_no += 1
        elif cur and line[:1] == "-":
            cur[3].append(line[1:])
            cur[4].append(old_no)
            old_no += 1
        elif cur and line[:1] == "+":
            cur[4].append(old_no)
    if cur:
        yield cur


def enclosing(tree_ast, start, end):
    """Innermost (function, class) nodes spanning lines start..end."""
    func = cls = None
    for node in ast.walk(tree_ast):
        if not hasattr(node, "end_lineno") or not (node.lineno <= start and end <= node.end_lineno):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if func is None or node.lineno >= func.lineno:
                func = node
        elif isinstance(node, ast.ClassDef):
            if cls is None or node.lineno >= cls.lineno:
                cls = node
    return func, cls


def snippet(lines, a, b):
    return "\n".join(f"{i:5} {lines[i - 1]}" for i in range(a, min(b, len(lines)) + 1))


def grep(base_tree, pattern, is_test, exclude_line=None):
    p = subprocess.run(["grep", "-rnE", "--include=*.py", pattern, "."], cwd=base_tree,
                       capture_output=True, text=True)
    return [h[2:] for h in p.stdout.splitlines()
            if not is_test(h[2:].split(":", 1)[0]) and h[2:] != exclude_line]


def facts(diff, base_tree, is_test=lambda path: False):
    base_tree = Path(base_tree)
    out = []
    defs, seen_defs, names, classes = [], set(), {}, {}
    for path, start, length, old_lines, changed in hunks(diff):
        end = start + max(length, 1) - 1
        src = base_tree / path
        if not path.endswith(".py") or is_test(path) or not src.is_file() or not length:
            continue
        text = src.read_text(errors="replace")
        try:
            t = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        # The definition around the edited lines; the whole hunk only when no definition
        # spans the edit (context lines often run past the end of a function).
        func = cls = None
        if changed:
            func, cls = enclosing(t, min(changed), min(max(changed), len(lines)))
            if not func and not cls:
                func, cls = enclosing(t, min(changed), min(changed))
        if not func and not cls:
            func, cls = enclosing(t, start, end)
        if func:
            key = (path, func.lineno)
            if key not in seen_defs:
                seen_defs.add(key)
                a, b = func.lineno, func.end_lineno
                if b - a > MAX_DEF_LINES:
                    a, b = max(a, start - 40), min(b, end + 40)
                owner = f" in class {cls.name}" if cls and cls.lineno < func.lineno else ""
                defs.append(f"#### {path}: {func.name}{owner}\n```python\n{snippet(lines, a, b)}\n```")
            names.setdefault(func.name, f"{path}:{func.lineno}:{lines[func.lineno - 1]}")
        if cls:
            classes.setdefault(cls.name, path)
            if not func and (path, cls.lineno, start) not in seen_defs:
                seen_defs.add((path, cls.lineno, start))
                head = snippet(lines, cls.lineno, cls.lineno + 2)
                body = snippet(lines, max(cls.lineno + 3, start - 15), end + 15)
                defs.append(f"#### {path}: class {cls.name} (header and changed region)\n```python\n{head}\n ...\n{body}\n```")
    out += ["### Definitions around the changes (code before this change)"] + (defs or ["(none: no Python code changed)"])
    refs_out = []
    for cname in classes:
        hits = grep(base_tree, rf"^\s*class \w+\([^)]*\b{re.escape(cname)}\b", is_test)
        if hits:
            refs_out.append(f"- subclasses of {cname} ({len(hits)}): " + "; ".join(
                h.split(":", 2)[0] + ": " + h.split(":", 2)[2].strip() for h in hits[:15]))
    for fname, def_line in names.items():
        if fname.startswith("__") or fname in GENERIC or len(fname) < 4:
            continue
        hits = grep(base_tree, rf"\b{re.escape(fname)}\b", is_test, exclude_line=def_line)
        if hits:
            shown = "\n".join(f"    {h[:200]}" for h in hits[:MAX_HITS])
            more = f"\n    ... {len(hits) - MAX_HITS} more" if len(hits) > MAX_HITS else ""
            refs_out.append(f"- references to {fname} ({len(hits)} outside tests):\n{shown}{more}")
    out += ["", "### Who else uses the changed code (non-test references, code before this change)"] + (refs_out or ["(none found)"])
    text = "\n".join(out)
    if len(text) > MAX_FACT_CHARS:
        text = text[:MAX_FACT_CHARS] + f"\n[... repository facts truncated at {MAX_FACT_CHARS} chars ...]"
    return text
