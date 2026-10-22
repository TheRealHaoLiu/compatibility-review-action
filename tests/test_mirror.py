import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from compat_review import mirror


def bare(path, megabytes, age_seconds):
    """A bare repository holding `megabytes` of junk, last fetched `age_seconds` ago."""
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    (path / "objects" / "pack").mkdir(parents=True, exist_ok=True)
    (path / "objects" / "pack" / "junk.pack").write_bytes(b"\0" * (megabytes * 1024 * 1024))
    fetched = path / "FETCH_HEAD"
    fetched.write_text("")
    when = time.time() - age_seconds
    os.utime(fetched, (when, when))
    return path


class MirrorTest(unittest.TestCase):
    def test_budget_evicts_least_recently_used_until_it_fits(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "mirrors"
            old = bare(root / "o-old.git", 3, 3000)
            mid = bare(root / "o-mid.git", 2, 2000)
            new = bare(root / "o-new.git", 1, 1000)
            (root / "notes").mkdir()  # not a mirror: neither counted nor deleted
            (root / "notes" / "x").write_bytes(b"\0" * 1024 * 1024)
            deleted, total = mirror.enforce_budget(data, 4 * 1024 * 1024)
            self.assertEqual(deleted, [old])
            self.assertFalse(old.exists())
            self.assertTrue(mid.exists() and new.exists() and (root / "notes").exists())
            self.assertLessEqual(total, 4 * 1024 * 1024)
            # under budget: nothing happens
            self.assertEqual(mirror.enforce_budget(data, 4 * 1024 * 1024), ([], total))
            # `keep` survives even when it is the oldest
            deleted, _ = mirror.enforce_budget(data, 1024 * 1024, keep=mid)
            self.assertEqual(deleted, [new])
            self.assertTrue(mid.exists())

    def test_only_bare_repositories_directly_under_mirrors_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "mirrors"
            real = bare(root / "o-r.git", 1, 10)
            elsewhere = bare(data / "elsewhere.git", 1, 10)
            os.symlink(elsewhere, root / "o-link.git")
            (root / "o-plain.git").mkdir()  # no HEAD, no objects
            self.assertTrue(mirror.is_mirror(data, real))
            self.assertFalse(mirror.is_mirror(data, root / "o-link.git"))
            self.assertFalse(mirror.is_mirror(data, root / "o-plain.git"))
            self.assertFalse(mirror.is_mirror(data, elsewhere))
            deleted, _ = mirror.enforce_budget(data, 0)
            self.assertEqual(deleted, [real])
            self.assertTrue(elsewhere.exists() and (root / "o-plain.git").exists())
            self.assertTrue((root / "o-link.git").is_symlink())

    def test_no_mirrors_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(mirror.enforce_budget(Path(tmp), 1), ([], 0))

    def test_release_pull_drops_the_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
            subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t",
                            "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            subprocess.run(["git", "-C", str(work), "update-ref", "refs/pull/7/head", "HEAD"], check=True)
            mirror.release_pull(work, 7)
            refs = subprocess.run(["git", "-C", str(work), "for-each-ref", "refs/pull"], capture_output=True, text=True)
            self.assertEqual(refs.stdout, "")


if __name__ == "__main__":
    unittest.main()
