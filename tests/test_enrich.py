import tempfile
import textwrap
import unittest
from pathlib import Path

from compat_review import enrich, scope

SOURCE = textwrap.dedent('''\
    import os


    def load_allow_list(settings):
        entries = []
        for raw in settings.get("ALLOW_LIST", []):
            entries.append(raw.strip())
        return entries


    def send(settings, target):
        if target not in load_allow_list(settings):
            raise ValueError(target)
        return True
''')

DIFF = textwrap.dedent('''\
    diff --git a/app/allow.py b/app/allow.py
    --- a/app/allow.py
    +++ b/app/allow.py
    @@ -6,3 +6,5 @@ def load_allow_list(settings):
             entries.append(raw.strip())
    +    if any(e.startswith("169.254.") for e in entries):
    +        raise ValueError("link-local is never allowed")
         return entries
''')


class EnrichTest(unittest.TestCase):
    def test_hunks(self):
        (path, start, length, old, changed), = list(enrich.hunks(DIFF))
        self.assertEqual((path, start, length), ("app/allow.py", 6, 3))
        self.assertEqual(len(old), 2)
        self.assertEqual(changed, [7, 7])  # two lines inserted before old line 7

    def test_definition_found_when_context_runs_past_the_function(self):
        diff = textwrap.dedent('''\
            diff --git a/app/allow.py b/app/allow.py
            --- a/app/allow.py
            +++ b/app/allow.py
            @@ -4,6 +4,8 @@ def load_allow_list(settings):
                 for raw in settings.get("ALLOW_LIST", []):
                     entries.append(raw.strip())
            +    if any(e.startswith("169.254.") for e in entries):
            +        raise ValueError("link-local is never allowed")
                 return entries


        ''')
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app").mkdir()
            (Path(tmp) / "app" / "allow.py").write_text(SOURCE)
            text = enrich.facts(diff, tmp)
        self.assertIn("#### app/allow.py: load_allow_list", text)

    def test_facts_show_definition_and_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app").mkdir()
            (Path(tmp) / "app" / "allow.py").write_text(SOURCE)
            (Path(tmp) / "app" / "tests").mkdir()
            (Path(tmp) / "app" / "tests" / "test_allow.py").write_text("from app.allow import load_allow_list\n")
            text = enrich.facts(DIFF, tmp, lambda p: scope.is_test(p, []))
        self.assertIn("#### app/allow.py: load_allow_list", text)
        self.assertIn("references to load_allow_list (1 outside tests)", text)
        self.assertIn("app/allow.py:", text)
        self.assertNotIn("test_allow.py", text)

    def test_out_of_scope(self):
        diff = "diff --git a/docs/a.md b/docs/a.md\n" + "diff --git a/.github/x.yml b/.github/x.yml\n"
        self.assertEqual(scope.out_of_scope(diff, ["docs/**", ".github/**"]), ["docs/a.md", ".github/x.yml"])
        self.assertIsNone(scope.out_of_scope(diff + "diff --git a/app/a.py b/app/a.py\n", ["docs/**"]))


if __name__ == "__main__":
    unittest.main()
