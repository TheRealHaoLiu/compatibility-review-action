import hashlib
import hmac
import json
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

    def git_auth_header(self):
        return ""

    def pull(self, owner, repo, number):
        return {"body": self.body, "base": {"ref": "main"}, "title": "t"}

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

    def test_draft_skipped(self):
        settings = service.Settings(data_dir=Path("/nonexistent"), model="m")
        job = service.Job("o", "r", 1, "main", "t", "", draft=True)
        self.assertIsNone(service.handle(job, settings, FakeGitHub(), fake_reviewer, mirror="/nonexistent"))

    def test_webhook_endpoint(self):
        import queue
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        class StubWorker:
            queue = queue.Queue()

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
