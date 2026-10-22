import hashlib
import hmac
import json
import queue
import subprocess
import tempfile
import unittest
from pathlib import Path

from compat_review import post, service


def run(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def make_repo(tmp):
    """A bare 'mirror' with main and refs/pull/1/head, built from a working clone."""
    work = Path(tmp) / "work"
    work.mkdir()
    run(work, "init", "-q", "-b", "main")
    (work / "app").mkdir()
    (work / "app" / "allow.py").write_text("def load(settings):\n    return list(settings.get('ALLOW', []))\n")
    (work / ".compatibility-review.toml").write_text('ignore = ["docs/**"]\ncontext = "A product."\n')
    run(work, "add", "-A")
    run(work, "commit", "-q", "-m", "base")
    run(work, "checkout", "-q", "-b", "feature")
    (work / "app" / "allow.py").write_text(
        "def load(settings):\n    out = list(settings.get('ALLOW', []))\n    if any(e.startswith('169.254.') for e in out):\n"
        "        raise ValueError('link-local')\n    return out\n")
    run(work, "commit", "-qam", "deny link-local")
    run(work, "update-ref", "refs/pull/1/head", "feature")
    run(work, "checkout", "-q", "main")
    run(work, "checkout", "-q", "-b", "docs-only")
    (work / "docs").mkdir()
    (work / "docs" / "a.md").write_text("hi\n")
    run(work, "add", "-A")
    run(work, "commit", "-qm", "docs")
    run(work, "update-ref", "refs/pull/2/head", "docs-only")
    return work


class FakeGitHub:
    def __init__(self):
        self.body = "Original description."
        self.comments = []
        self.pulls = []

    def git_auth_header(self):
        return ""

    def pull(self, owner, repo, number):
        return {"body": self.body, "base": {"ref": "main"}, "title": "t"}

    def open_pulls(self, owner, repo):
        return list(self.pulls)

    def update_pull_body(self, owner, repo, number, body):
        self.body = body
        return {}

    def create_comment(self, owner, repo, number, body):
        self.comments.append(body)
        return {"id": len(self.comments)}


def fake_reviewer(user, context, model, api, base_url, effort):
    assert "## Repository facts" in user and "#### app/allow.py: load" in user
    assert context.startswith("A product.")
    return {"findings": [], "summary": "Link-local entries in a stored allow list now raise at load.",
            "q_changes_existing_path": "yes", "q_breaking_setup": "A stored ALLOW entry for 169.254.x",
            "q_mechanism": "restricts_stored_at_runtime", "q_prevalence": "rare", "q_auto_preserved": "no",
            "q_widens_access_or_changes_output": "no", "verdict": "breaking", "color": "Red",
            "_meta": {"prompt": "v9", "rule": "v9", "model": model, "api": api, "usage": {"in": 1, "out": 1}}}


class ServiceTest(unittest.TestCase):
    def test_signature(self):
        body = b'{"x":1}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(service.verify("s3cret", body, sig))
        self.assertFalse(service.verify("s3cret", body, "sha256=" + "0" * 64))
        self.assertFalse(service.verify("s3cret", body, None))
        self.assertTrue(service.verify(None, body, None))

    def test_parse_event(self):
        payload = {"action": "synchronize", "pull_request": {"number": 7, "base": {"ref": "main"}, "title": "T",
                   "body": None, "draft": False}, "repository": {"name": "r", "owner": {"login": "o"}},
                   "installation": {"id": 42}}
        job = service.parse_event("pull_request", payload)
        self.assertEqual((job.owner, job.repo, job.number, job.base_ref, job.body, job.installation_id),
                         ("o", "r", 7, "main", "", 42))
        self.assertIsNone(service.parse_event("pull_request", {**payload, "action": "labeled"}))
        self.assertIsNone(service.parse_event("push", payload))

    def test_splice_and_record(self):
        result = fake_reviewer("## Repository facts\n#### app/allow.py: load", "A product.", "m", "responses", None, "medium")
        result["_meta"].update({"head": "abcdef1234567", "base": "1234567abcdef", "diff_sha256": "ff" * 32})
        once = post.splice("Original description.", post.note(result))
        twice = post.splice(once, post.note(result))
        self.assertEqual(once, twice)
        self.assertIn("🔴 Red", once)
        self.assertTrue(once.startswith("Original description."))
        rec = post.parse_record(post.comment(result))
        self.assertEqual((rec["verdict"], rec["head"], rec["version"]), ("breaking", "abcdef1234567", "v9"))

    def test_handle_reviews_posts_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = make_repo(tmp)
            settings = service.Settings(data_dir=Path(tmp) / "data", model="m", post=True)
            gh = FakeGitHub()
            job = service.Job("o", "r", 1, "main", "Deny link-local", "")
            result = service.handle(job, settings, gh, fake_reviewer, mirror=work)
            self.assertEqual(result["verdict"], "breaking")
            self.assertIn("🔴 Red", gh.body)
            self.assertEqual(len(gh.comments), 1)
            self.assertEqual(post.parse_record(gh.comments[0])["diff_sha256"], result["_meta"]["diff_sha256"])
            self.assertEqual(result["_meta"]["comment_id"], 1)
            # same diff again: cached, nothing posted
            self.assertIsNone(service.handle(job, settings, gh, fake_reviewer, mirror=work))
            self.assertEqual(len(gh.comments), 1)
            # a change touching only ignored paths is Green without the reviewer
            job2 = service.Job("o", "r", 2, "main", "docs", "")
            r2 = service.handle(job2, settings, gh, lambda *a: self.fail("model called"), mirror=work)
            self.assertEqual(r2["verdict"], "none")
            self.assertEqual(len(gh.comments), 2)

    def test_stored_without_posting_is_posted_later_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = make_repo(tmp)
            gh = FakeGitHub()
            job = service.Job("o", "r", 1, "main", "Deny link-local", "")
            quiet = service.Settings(data_dir=Path(tmp) / "data", model="m", post=False)
            first = service.handle(job, quiet, gh, fake_reviewer, mirror=work)
            self.assertEqual(gh.comments, [])
            self.assertNotIn("comment_id", first["_meta"])
            loud = service.Settings(data_dir=Path(tmp) / "data", model="m", post=True)
            again = service.handle(job, loud, gh, lambda *a: self.fail("model called"), mirror=work)
            self.assertEqual(again["_meta"]["comment_id"], 1)
            self.assertIn("🔴 Red", gh.body)
            stored = json.loads(next((Path(tmp) / "data" / "reviews" / "o-r").glob("1-*.json")).read_text())
            self.assertEqual(stored["_meta"]["comment_id"], 1)
            self.assertIsNone(service.handle(job, loud, gh, lambda *a: self.fail("model called"), mirror=work))
            self.assertEqual(len(gh.comments), 1)

    def test_draft_skipped(self):
        settings = service.Settings(data_dir=Path("/nonexistent"), model="m")
        job = service.Job("o", "r", 1, "main", "t", "", draft=True)
        self.assertIsNone(service.handle(job, settings, FakeGitHub(), fake_reviewer, mirror="/nonexistent"))

    def test_fetched_pull_ref_is_released_and_tree_extracted_under_data(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            work = make_repo(tmp)
            data = Path(tmp) / "data"
            settings = service.Settings(data_dir=data, model="m", post=False)
            seen = {}
            real_extract = service.extract_tree

            def spy_extract(repo, ref, dest):
                seen["dest"] = Path(dest)
                real_extract(repo, ref, dest)

            with mock.patch.object(service.mirrors, "ensure", return_value=work), \
                    mock.patch.object(service.mirrors, "fetch_pull", return_value="refs/pull/1/head"), \
                    mock.patch.object(service, "extract_tree", side_effect=spy_extract):
                result = service.handle(service.Job("o", "r", 1, "main", "t", ""), settings, FakeGitHub(), fake_reviewer)
            self.assertEqual(result["verdict"], "breaking")
            self.assertEqual(seen["dest"].resolve().parent, (data / "tmp").resolve())
            self.assertTrue(seen["dest"].name.startswith("tree-"))
            self.assertEqual(list((data / "tmp").iterdir()), [])  # removed before the model call, never held through it
            refs = subprocess.run(["git", "-C", str(work), "for-each-ref", "refs/pull/1"], capture_output=True, text=True)
            self.assertEqual(refs.stdout, "")  # released even though posting was off
            # a review that fails still releases the ref
            run(work, "update-ref", "refs/pull/1/head", "feature")
            with mock.patch.object(service.mirrors, "ensure", return_value=work), \
                    mock.patch.object(service.mirrors, "fetch_pull", return_value="refs/pull/1/head"), \
                    self.assertRaises(RuntimeError):
                service.handle(service.Job("o", "r", 1, "nonexistent-branch", "t", ""), settings, FakeGitHub(), fake_reviewer)
            refs = subprocess.run(["git", "-C", str(work), "for-each-ref", "refs/pull/1"], capture_output=True, text=True)
            self.assertEqual(refs.stdout, "")

    def test_data_lock_is_exclusive_across_handles(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            with service.DataLock(tmp):
                other = open(Path(tmp) / ".lock", "a+")
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    other.close()
            with service.DataLock(tmp):  # released: can be taken again
                pass

    def test_clear_tmp_removes_only_extraction_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = service.Settings(data_dir=Path(tmp), model="m")
            t = service.tmp_dir(settings)
            (t / "tree-abc").mkdir()
            (t / "tree-abc" / "f").write_text("x")
            (t / "tree-def").mkdir()
            (t / "other").mkdir()
            (t / "tree-file").write_text("not a directory")
            self.assertEqual(service.clear_tmp(settings), 2)
            self.assertEqual(sorted(p.name for p in t.iterdir()), ["other", "tree-file"])

    def test_parse_repos(self):
        self.assertEqual(service.parse_repos(["o/r", "a/b, c/d"]), [("o", "r"), ("a", "b"), ("c", "d")])
        self.assertEqual(service.parse_repos([""]), [])
        with self.assertRaises(ValueError):
            service.parse_repos(["just-a-name"])

    def test_poller_leaves_stale_pull_requests_alone_until_pushed(self):
        from datetime import datetime, timedelta, timezone

        def pr(number, sha, days_ago):
            when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"number": number, "head": {"sha": sha}, "base": {"ref": "main"}, "title": f"PR {number}",
                    "body": "", "draft": False, "updated_at": when}

        class StubWorker:
            pass

        StubWorker.queue = queue.Queue()
        gh = FakeGitHub()
        gh.pulls = [pr(1, "aaa", 2), pr(2, "bbb", 400), pr(3, "ccc", 31)]
        with tempfile.TemporaryDirectory() as tmp:
            settings = service.Settings(data_dir=Path(tmp), model="m")
            poller = service.Poller(settings, StubWorker, [("o", "r")], 60, client=lambda s, o, r: (gh, None),
                                    max_age_days=30)
            with self.assertLogs("compatibility-review", level="INFO") as logs:
                self.assertEqual([j.number for j in poller.scan()], [1])
            self.assertIn("#2, #3", logs.output[0])
            self.assertEqual(poller.scan(), [])  # stale ones are not re-examined every scan
            gh.pulls = [pr(1, "aaa", 2), pr(2, "bb2", 0), pr(3, "ccc", 31)]  # a push revives #2
            self.assertEqual([j.number for j in poller.scan()], [2])
            # no limit reviews everything
            everything = service.Poller(settings, StubWorker, [("o", "r")], 60, client=lambda s, o, r: (gh, None),
                                        max_age_days=0)
            self.assertEqual([j.number for j in everything.scan()], [1, 2, 3])

    def test_age_days(self):
        from datetime import datetime, timezone

        now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.assertAlmostEqual(service.age_days("2026-10-12T00:00:00Z", now), 10.0)
        self.assertIsNone(service.age_days(None))

    def test_poller_queues_only_new_heads(self):
        def pr(number, sha, draft=False):
            return {"number": number, "head": {"sha": sha}, "base": {"ref": "main"}, "title": f"PR {number}",
                    "body": None, "draft": draft}

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            reviews = data / "reviews" / "o-r"
            reviews.mkdir(parents=True)
            (reviews / "3-aaaaaaaaaaaa.json").write_text(json.dumps(
                {"verdict": "none", "_meta": {"number": 3, "head": "ccc", "comment_id": 9}}))
            (reviews / "4-bbbbbbbbbbbb.json").write_text(json.dumps(
                {"verdict": "none", "_meta": {"number": 4, "head": "ddd"}}))  # stored while posting was off
            gh = FakeGitHub()
            gh.pulls = [pr(1, "aaa"), pr(2, "bbb", draft=True), pr(3, "ccc"), pr(4, "ddd")]

            class StubWorker:
                pass

            StubWorker.queue = queue.Queue()
            settings = service.Settings(data_dir=data, model="m", post=True)
            poller = service.Poller(settings, StubWorker, [("o", "r")], 60, client=lambda s, o, r: (gh, 42))
            queued = poller.scan()
            self.assertEqual([j.number for j in queued], [1, 4])  # 2 is a draft, 3 was reviewed and posted
            self.assertEqual((queued[0].installation_id, queued[0].base_ref, queued[0].title), (42, "main", "PR 1"))
            self.assertEqual(StubWorker.queue.qsize(), 2)
            self.assertEqual(poller.scan(), [])
            gh.pulls = [pr(1, "aa2"), pr(2, "bbb"), pr(3, "ccc")]  # 1 pushed, 2 ready for review, 4 closed
            self.assertEqual([j.number for j in poller.scan()], [1, 2])
            self.assertEqual(poller.scan(), [])

    def test_poller_survives_a_listing_failure(self):
        class StubWorker:
            pass

        StubWorker.queue = queue.Queue()

        def broken(settings, owner, repo):
            raise service.GitHubError("GET /repos/o/r/pulls: 404 Not Found")

        settings = service.Settings(data_dir=Path("/nonexistent"), model="m")
        poller = service.Poller(settings, StubWorker, [("o", "r")], 60, client=broken)
        with self.assertLogs("compatibility-review", level="ERROR"):
            self.assertEqual(poller.scan(), [])

    def test_webhook_endpoint(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        class StubWorker:
            pass

        StubWorker.queue = queue.Queue()

        settings = service.Settings(data_dir=Path("/nonexistent"), model="m", webhook_secret="s3cret")
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(settings, StubWorker))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/healthz") as r:
                self.assertEqual(r.status, 200)
            payload = json.dumps({"action": "opened", "pull_request": {"number": 3, "base": {"ref": "main"},
                                  "title": "T", "body": "", "draft": False},
                                  "repository": {"name": "r", "owner": {"login": "o"}},
                                  "installation": {"id": 1}}).encode()
            sig = "sha256=" + hmac.new(b"s3cret", payload, hashlib.sha256).hexdigest()
            req = urllib.request.Request(base + "/webhook", data=payload, headers={
                "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig, "Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 202)
            job = StubWorker.queue.get(timeout=2)
            self.assertEqual((job.owner, job.repo, job.number), ("o", "r", 3))
            bad = urllib.request.Request(base + "/webhook", data=payload, headers={
                "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=" + "0" * 64})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad)
            self.assertEqual(ctx.exception.code, 401)
            ping = urllib.request.Request(base + "/webhook", data=b"{}", headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": "sha256=" + hmac.new(b"s3cret", b"{}", hashlib.sha256).hexdigest()})
            with urllib.request.urlopen(ping) as r:
                self.assertEqual(r.status, 200)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
