"""The webhook service: a GitHub App that reviews pull requests as they open and change.

    compatibility-review-service [--port 8000] [--data ./data] [--no-post]

GitHub sends `pull_request` events to POST /webhook. The service verifies the signature, queues
the pull request, and a worker reviews it: fetch the mirror, diff from the merge base, repository
facts from the tree before the change, one model call, then the note in the PR description and
the gradable comment. A diff already reviewed (same sha256) is not reviewed again.

Environment:
  GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (PEM) or GITHUB_APP_PRIVATE_KEY_FILE, GITHUB_WEBHOOK_SECRET
  GITHUB_TOKEN              a personal token instead of the App, for a trial without an App
  COMPAT_REVIEW_MODEL, COMPAT_REVIEW_API, COMPAT_REVIEW_EFFORT, OPENAI_BASE_URL, OPENAI_API_KEY
  COMPAT_REVIEW_DATA        data directory (mirrors, reviews); default ./data
  COMPAT_REVIEW_SKIP_DRAFTS "0" to review draft pull requests too
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import PROMPT_VERSION, RULE_VERSION, enrich, post, review, scope
from .change import build_input, diff_from_merge_base, extract_tree, read_config, context_file
from .github import GitHub
from . import mirror as mirrors

log = logging.getLogger("compatibility-review")
ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@dataclass
class Settings:
    data_dir: Path
    model: str
    api: str = "responses"
    effort: str = "medium"
    base_url: str | None = None
    post: bool = True
    skip_drafts: bool = True
    app_id: str | None = None
    private_key: str | None = None
    webhook_secret: str | None = None
    token: str | None = None

    @classmethod
    def from_env(cls, data_dir=None, post=True):
        key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        if not key and os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE"):
            key = Path(os.environ["GITHUB_APP_PRIVATE_KEY_FILE"]).read_text()
        return cls(data_dir=Path(data_dir or os.environ.get("COMPAT_REVIEW_DATA", "data")),
                   model=os.environ.get("COMPAT_REVIEW_MODEL", ""),
                   api=os.environ.get("COMPAT_REVIEW_API", "responses"),
                   effort=os.environ.get("COMPAT_REVIEW_EFFORT", "medium"),
                   base_url=os.environ.get("COMPAT_REVIEW_BASE_URL"), post=post,
                   skip_drafts=os.environ.get("COMPAT_REVIEW_SKIP_DRAFTS", "1") != "0",
                   app_id=os.environ.get("GITHUB_APP_ID"), private_key=key,
                   webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
                   token=os.environ.get("GITHUB_TOKEN"))


@dataclass
class Job:
    owner: str
    repo: str
    number: int
    base_ref: str
    title: str
    body: str
    installation_id: int | None = None
    draft: bool = False


def verify(secret, body, signature_header):
    """GitHub's X-Hub-Signature-256 over the raw body."""
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature_header[7:])


def parse_event(event, payload):
    """A Job for a pull_request event the service acts on, else None."""
    if event != "pull_request" or payload.get("action") not in ACTIONS:
        return None
    pr = payload["pull_request"]
    repo = payload["repository"]
    return Job(owner=repo["owner"]["login"], repo=repo["name"], number=pr["number"],
               base_ref=pr["base"]["ref"], title=pr.get("title") or "", body=pr.get("body") or "",
               installation_id=(payload.get("installation") or {}).get("id"), draft=bool(pr.get("draft")))


def client(settings, job):
    if settings.token:
        return GitHub(settings.token)
    if not (settings.app_id and settings.private_key and job.installation_id):
        raise RuntimeError("no GITHUB_TOKEN and no App credentials for this installation")
    return GitHub.as_installation(settings.app_id, settings.private_key, job.installation_id)


def reviews_dir(settings, job):
    d = settings.data_dir / "reviews" / f"{job.owner}-{job.repo}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def handle(job, settings, gh=None, reviewer=review.review, mirror=None):
    """Review one pull request end to end. Returns the result, or None when nothing was reviewed."""
    if job.draft and settings.skip_drafts:
        log.info("%s/%s#%s: draft, skipped", job.owner, job.repo, job.number)
        return None
    gh = gh or client(settings, job)
    if mirror is None:
        auth = gh.git_auth_header()
        mirror = mirrors.ensure(settings.data_dir, job.owner, job.repo, auth)
        head_ref = mirrors.fetch_pull(mirror, job.number, auth)
    else:
        head_ref = f"refs/pull/{job.number}/head"
    merge_base, head_sha, diff = diff_from_merge_base(mirror, f"refs/heads/{job.base_ref}", head_ref)
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    out = reviews_dir(settings, job) / f"{job.number}-{diff_sha[:12]}.json"
    if out.exists():
        log.info("%s/%s#%s: diff %s already reviewed", job.owner, job.repo, job.number, diff_sha[:12])
        return None

    cfg = tomllib.loads(read_config(mirror, merge_base))
    ignore = cfg.get("ignore", [])
    skipped = scope.out_of_scope(diff, ignore)
    if skipped:
        result = scope.skipped_result(skipped)
        result["_meta"].update({"prompt": PROMPT_VERSION, "rule": RULE_VERSION, "model": settings.model})
    else:
        with tempfile.TemporaryDirectory(prefix="compat-review-") as tmp:
            extract_tree(mirror, merge_base, tmp)
            facts = enrich.facts(diff, tmp, lambda p: scope.is_test(p, ignore))
        user = build_input(job.base_ref, job.title, job.body, diff, facts)
        if cfg.get("context"):
            context = cfg["context"].strip() + "\n"
        elif cfg.get("context_file"):
            context = context_file(mirror, merge_base, cfg["context_file"])
        else:
            context = (Path(__file__).resolve().parent / "context.md").read_text()
        result = reviewer(user, context, settings.model, settings.api, settings.base_url, settings.effort)
    result["_meta"].update({"owner": job.owner, "repo": job.repo, "number": job.number,
                            "base": merge_base, "head": head_sha, "diff_sha256": diff_sha})

    if settings.post:
        pr = gh.pull(job.owner, job.repo, job.number)
        gh.update_pull_body(job.owner, job.repo, job.number, post.splice(pr.get("body"), post.note(result)))
        c = gh.create_comment(job.owner, job.repo, job.number, post.comment(result))
        result["_meta"]["comment_id"] = c.get("id")
    out.write_text(json.dumps(result, indent=1) + "\n")
    log.info("%s/%s#%s: %s (%s)", job.owner, job.repo, job.number, result["verdict"], diff_sha[:12])
    return result


class Worker(threading.Thread):
    def __init__(self, settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.queue = queue.Queue()

    def run(self):
        while True:
            job = self.queue.get()
            try:
                handle(job, self.settings)
            except Exception:  # noqa: BLE001 - one bad PR must not stop the worker
                log.exception("%s/%s#%s failed", job.owner, job.repo, job.number)
            finally:
                self.queue.task_done()


def make_handler(settings, worker):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _reply(self, code, text):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                return self._reply(200, "ok\n")
            self._reply(404, "not found\n")

        def do_POST(self):
            if self.path != "/webhook":
                return self._reply(404, "not found\n")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not verify(settings.webhook_secret, body, self.headers.get("X-Hub-Signature-256")):
                return self._reply(401, "bad signature\n")
            try:
                job = parse_event(self.headers.get("X-GitHub-Event"), json.loads(body))
            except (ValueError, KeyError):
                return self._reply(400, "bad payload\n")
            if job is None:
                return self._reply(200, "ignored\n")
            worker.queue.put(job)
            self._reply(202, f"queued {job.owner}/{job.repo}#{job.number}\n")

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(prog="compatibility-review-service", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--data", help="data directory (default: COMPAT_REVIEW_DATA or ./data)")
    ap.add_argument("--no-post", action="store_true", help="review and store results, write nothing to GitHub")
    ap.add_argument("--once", metavar="OWNER/REPO#N", help="review one pull request now and exit; no server")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env(args.data, post=not args.no_post)
    if not settings.model:
        ap.error("COMPAT_REVIEW_MODEL is required")

    if args.once:
        repo_part, number = args.once.rsplit("#", 1)
        owner, repo = repo_part.split("/", 1)
        try:
            gh = client(settings, Job(owner, repo, int(number), "", "", ""))
            pr = gh.pull(owner, repo, int(number))
            job = Job(owner, repo, int(number), pr["base"]["ref"], pr.get("title") or "", pr.get("body") or "",
                      draft=bool(pr.get("draft")))
            result = handle(job, settings, gh)
        except (RuntimeError, OSError) as e:
            ap.exit(1, f"{type(e).__name__}: {str(e)[:400]}\n")
        print(json.dumps(result, indent=1) if result else "nothing reviewed")
        return 0

    if not settings.webhook_secret:
        log.warning("GITHUB_WEBHOOK_SECRET is not set: signatures are not checked")
    worker = Worker(settings)
    worker.start()
    server = ThreadingHTTPServer(("", args.port), make_handler(settings, worker))
    log.info("listening on :%s, posting %s, data in %s", args.port, "on" if settings.post else "off", settings.data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
