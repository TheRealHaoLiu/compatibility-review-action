"""The service: reviews pull requests as they open and change, by webhook, by polling, or both.

    compatibility-review-service [--port 8000] [--data ./data] [--no-post]            webhook mode
    compatibility-review-service --poll OWNER/REPO [--interval 60] [--no-webhook]      poll mode
    compatibility-review-service --once OWNER/REPO#N                                   one PR, then exit

Webhook mode: GitHub sends `pull_request` events to POST /webhook; the service verifies the
signature and queues the pull request. Poll mode: every interval the service lists the open pull
requests of each named repository and queues those whose head commit it has not seen. Either way
one worker reviews the queue: fetch the mirror, diff from the merge base, repository facts from
the tree before the change, one model call, then the note in the PR description and the gradable
comment. A diff already reviewed (same sha256) is not reviewed again; a result stored while
posting was off is posted, without a new model call, the next time it comes up with posting on.

Environment:
  GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY (PEM) or GITHUB_APP_PRIVATE_KEY_FILE, GITHUB_WEBHOOK_SECRET
  GITHUB_TOKEN              a personal token instead of the App; enough for poll mode and --once
  COMPAT_REVIEW_POLL_REPOS  repositories to poll, comma-separated (same as --poll)
  COMPAT_REVIEW_POLL_INTERVAL seconds between polls (same as --interval); default 60
  COMPAT_REVIEW_POLL_MAX_AGE_DAYS leave pull requests not updated in this many days alone until
                            pushed (same as --max-age); default 30, 0 for no limit
  COMPAT_REVIEW_MODEL, COMPAT_REVIEW_API, COMPAT_REVIEW_EFFORT, OPENAI_BASE_URL, OPENAI_API_KEY
  COMPAT_REVIEW_DATA        data directory (mirrors, reviews); default ./data
  COMPAT_REVIEW_MAX_MIRRORS_GB  budget for all mirrors together; least recently used ones are
                            deleted past it and cloned again when needed; default 50
  COMPAT_REVIEW_SKIP_DRAFTS "0" to review draft pull requests too
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import shutil
import sys
import tempfile
import threading
import tomllib
from dataclasses import dataclass

try:
    import fcntl
except ImportError:  # Windows: no cross-process lock; one process per data directory is on the operator
    fcntl = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import PROMPT_VERSION, RULE_VERSION, enrich, post, review, scope
from .change import build_input, diff_from_merge_base, extract_tree, read_config, context_file
from .github import GitHub, GitHubError
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
    max_mirror_bytes: int = 50 * 1024**3
    app_id: str | None = None
    private_key: str | None = None
    webhook_secret: str | None = None
    token: str | None = None

    @classmethod
    def from_env(cls, data_dir=None, post=True, max_mirrors_gb=None):
        key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        if not key and os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE"):
            key = Path(os.environ["GITHUB_APP_PRIVATE_KEY_FILE"]).read_text()
        gb = max_mirrors_gb if max_mirrors_gb is not None else float(os.environ.get("COMPAT_REVIEW_MAX_MIRRORS_GB", "50"))
        return cls(data_dir=Path(data_dir or os.environ.get("COMPAT_REVIEW_DATA", "data")),
                   model=os.environ.get("COMPAT_REVIEW_MODEL", ""),
                   api=os.environ.get("COMPAT_REVIEW_API", "responses"),
                   effort=os.environ.get("COMPAT_REVIEW_EFFORT", "medium"),
                   base_url=os.environ.get("COMPAT_REVIEW_BASE_URL"), post=post,
                   skip_drafts=os.environ.get("COMPAT_REVIEW_SKIP_DRAFTS", "1") != "0",
                   max_mirror_bytes=int(gb * 1024**3),
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


_clients = {}  # installation id -> GitHub, re-minted when the token is stale


def client_for_installation(settings, installation_id):
    if settings.token:
        return GitHub(settings.token)
    if not (settings.app_id and settings.private_key):
        raise RuntimeError("set GITHUB_TOKEN, or GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY(_FILE)")
    if not installation_id:
        raise RuntimeError("no installation id for this repository")
    gh = _clients.get(installation_id)
    if gh is None or gh.stale():
        gh = _clients[installation_id] = GitHub.as_installation(settings.app_id, settings.private_key, installation_id)
    return gh


def client(settings, job):
    return client_for_installation(settings, job.installation_id)


def client_for_repo(settings, owner, repo):
    """(client, installation id) for a repository named on the command line rather than in an event."""
    if settings.token:
        return GitHub(settings.token), None
    if not (settings.app_id and settings.private_key):
        raise RuntimeError("set GITHUB_TOKEN, or GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY(_FILE)")
    installation_id = GitHub.installation_id(settings.app_id, settings.private_key, owner, repo)
    return client_for_installation(settings, installation_id), installation_id


def reviews_dir(settings, owner, repo):
    d = settings.data_dir / "reviews" / f"{owner}-{repo}"
    d.mkdir(parents=True, exist_ok=True)
    return d


class DataLock:
    """One review at a time per data directory, across processes.

    The worker, a `--once` run, and the startup cleanup all take it, so a review by hand alongside
    the running service waits for the current review instead of racing its extracted tree or
    evicting its mirror, and two services on one data directory take turns.
    """

    def __init__(self, data_dir):
        self.path = Path(data_dir) / ".lock"
        self.f = None

    def __enter__(self):
        if fcntl is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")  # noqa: SIM115 - closed in __exit__
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("waiting for another review on %s", self.path.parent)
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
            self.f = None


def tmp_dir(settings):
    """Where trees are extracted: under the data directory, so a hard kill leaks nothing elsewhere."""
    d = settings.data_dir / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clear_tmp(settings):
    """Remove extraction directories left by a previous process. Returns how many."""
    root = tmp_dir(settings).resolve()
    n = 0
    for p in root.iterdir():
        if p.name.startswith("tree-") and not p.is_symlink() and p.is_dir() and p.resolve().parent == root:
            shutil.rmtree(p)
            n += 1
    return n


def mirrors_size_gb(settings):
    root = settings.data_dir / "mirrors"
    return sum(mirrors.size(p) for p in root.iterdir() if mirrors.is_mirror(settings.data_dir, p)) / 1024**3 \
        if root.is_dir() else 0.0


def reviewed_heads(settings, owner, repo):
    """{number: head sha} of the stored reviews, so a restart does not fetch again what it reviewed.

    With posting on, a result stored while posting was off does not count: it still has to be posted.
    """
    out = {}
    d = settings.data_dir / "reviews" / f"{owner}-{repo}"
    files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime) if d.is_dir() else []
    for f in files:
        try:
            meta = json.loads(f.read_text()).get("_meta", {})
        except ValueError:
            continue
        if settings.post and "comment_id" not in meta:
            continue
        if meta.get("number") and meta.get("head"):
            out[meta["number"]] = meta["head"]
    return out


def publish(gh, job, result, out):
    """The note into the PR description and the gradable comment; the stored result records the comment."""
    pr = gh.pull(job.owner, job.repo, job.number)
    gh.update_pull_body(job.owner, job.repo, job.number, post.splice(pr.get("body"), post.note(result)))
    c = gh.create_comment(job.owner, job.repo, job.number, post.comment(result))
    result["_meta"]["comment_id"] = c.get("id")
    out.write_text(json.dumps(result, indent=1) + "\n")


def handle(job, settings, gh=None, reviewer=review.review, mirror=None):
    """Review one pull request end to end. Returns the result, or None when nothing was reviewed.

    With `mirror` given (tests), the PR head is expected at refs/pull/<n>/head and left in place.
    Otherwise, under the data directory's lock, the service fetches it into the repository's
    mirror, and whatever happens next, drops the ref afterwards and brings the mirrors back under
    budget.
    """
    if job.draft and settings.skip_drafts:
        log.info("%s/%s#%s: draft, skipped", job.owner, job.repo, job.number)
        return None
    gh = gh or client(settings, job)
    if mirror is not None:
        return _review(job, settings, gh, reviewer, mirror)
    with DataLock(settings.data_dir):
        auth = gh.git_auth_header()
        mirror = mirrors.ensure(settings.data_dir, job.owner, job.repo, auth)
        mirrors.fetch_pull(mirror, job.number, auth)
        try:
            return _review(job, settings, gh, reviewer, mirror)
        finally:
            try:
                mirrors.release_pull(mirror, job.number)
                deleted, total = mirrors.enforce_budget(settings.data_dir, settings.max_mirror_bytes, keep=mirror)
                if deleted:
                    log.warning("mirrors over budget: deleted %s, %.1f GB remain",
                                ", ".join(p.name for p in deleted), total / 1024**3)
            except Exception:  # noqa: BLE001 - housekeeping must not mask the review's outcome
                log.exception("%s/%s#%s: housekeeping failed", job.owner, job.repo, job.number)


def _review(job, settings, gh, reviewer, mirror):
    head_ref = f"refs/pull/{job.number}/head"
    merge_base, head_sha, diff = diff_from_merge_base(mirror, f"refs/heads/{job.base_ref}", head_ref)
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()
    out = reviews_dir(settings, job.owner, job.repo) / f"{job.number}-{diff_sha[:12]}.json"
    if out.exists():
        result = json.loads(out.read_text())
        if settings.post and "comment_id" not in result["_meta"]:
            publish(gh, job, result, out)
            log.info("%s/%s#%s: diff %s reviewed earlier, posted now", job.owner, job.repo, job.number, diff_sha[:12])
            return result
        log.info("%s/%s#%s: diff %s already reviewed", job.owner, job.repo, job.number, diff_sha[:12])
        return None

    cfg = tomllib.loads(read_config(mirror, merge_base))
    ignore = cfg.get("ignore", [])
    skipped = scope.out_of_scope(diff, ignore)
    if skipped:
        result = scope.skipped_result(skipped)
        result["_meta"].update({"prompt": PROMPT_VERSION, "rule": RULE_VERSION, "model": settings.model})
    else:
        with tempfile.TemporaryDirectory(prefix="tree-", dir=tmp_dir(settings)) as tmp:
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
    out.write_text(json.dumps(result, indent=1) + "\n")  # stored first: a failed post never costs a second model call
    if settings.post:
        publish(gh, job, result, out)
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


def age_days(iso_timestamp, now=None):
    """Days since a GitHub timestamp such as 2026-10-21T08:12:33Z; None when there is none."""
    if not iso_timestamp:
        return None
    from datetime import datetime, timezone

    then = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    return (now - then).total_seconds() / 86400


class Poller(threading.Thread):
    """Every interval, list each repository's open pull requests and queue those whose head moved.

    A head is remembered once queued, so a pull request that fails to review is not retried until
    it changes or the service restarts. Stored reviews seed the memory at the first scan. A pull
    request not updated for more than `max_age_days` is treated as abandoned: remembered, not
    queued, and reviewed only once a push moves its head.
    """

    def __init__(self, settings, worker, repos, interval, client=client_for_repo, max_age_days=30):
        super().__init__(daemon=True)
        self.settings, self.worker, self.repos, self.interval, self.client = settings, worker, repos, interval, client
        self.max_age_days = max_age_days
        self.seen = {}  # (owner, repo, number) -> head sha queued or already reviewed
        self.loaded = set()
        self.stop = threading.Event()

    def scan(self):
        """One pass over every repository. Returns the jobs queued."""
        queued = []
        for owner, repo in self.repos:
            try:
                gh, installation_id = self.client(self.settings, owner, repo)
                pulls = gh.open_pulls(owner, repo)
            except (GitHubError, RuntimeError, OSError) as e:
                log.error("%s/%s: listing pull requests failed: %s", owner, repo, str(e)[:300])
                continue
            if (owner, repo) not in self.loaded:
                for number, head in reviewed_heads(self.settings, owner, repo).items():
                    self.seen[(owner, repo, number)] = head
                self.loaded.add((owner, repo))
            stale = []
            for pr in pulls:
                key, head = (owner, repo, pr["number"]), pr["head"]["sha"]
                if self.seen.get(key) == head:
                    continue
                if pr.get("draft") and self.settings.skip_drafts:
                    continue  # not remembered: reviewed once it is ready
                self.seen[key] = head
                age = age_days(pr.get("updated_at"))
                if self.max_age_days and age is not None and age > self.max_age_days:
                    stale.append(pr["number"])
                    continue
                job = Job(owner, repo, pr["number"], pr["base"]["ref"], pr.get("title") or "", pr.get("body") or "",
                          installation_id=installation_id, draft=bool(pr.get("draft")))
                self.worker.queue.put(job)
                queued.append(job)
            if stale:
                log.info("%s/%s: %s pull request%s not updated in %s days, left alone until pushed: %s", owner, repo,
                         len(stale), "" if len(stale) == 1 else "s", self.max_age_days,
                         ", ".join(f"#{n}" for n in stale[:20]) + (" ..." if len(stale) > 20 else ""))
        return queued

    def run(self):
        while not self.stop.is_set():
            try:
                jobs = self.scan()
                if jobs:
                    log.info("poll: queued %s", ", ".join(f"{j.owner}/{j.repo}#{j.number}" for j in jobs))
            except Exception:  # noqa: BLE001 - keep polling
                log.exception("poll failed")
            self.stop.wait(self.interval)


def parse_repos(values):
    """['o/r', 'o2/r2,o3/r3'] -> [('o', 'r'), ('o2', 'r2'), ('o3', 'r3')]."""
    repos = []
    for value in values or []:
        for item in value.replace(",", " ").split():
            owner, _, repo = item.partition("/")
            if not (owner and repo):
                raise ValueError(f"expected OWNER/REPO, got {item!r}")
            repos.append((owner, repo))
    return repos


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
    ap.add_argument("--poll", metavar="OWNER/REPO", action="append",
                    help="poll this repository's open pull requests (repeat or comma-separate; "
                         "default: COMPAT_REVIEW_POLL_REPOS)")
    ap.add_argument("--interval", type=int, default=int(os.environ.get("COMPAT_REVIEW_POLL_INTERVAL", "60")),
                    help="seconds between polls (default: COMPAT_REVIEW_POLL_INTERVAL or 60)")
    ap.add_argument("--max-age", type=float, metavar="DAYS",
                    default=float(os.environ.get("COMPAT_REVIEW_POLL_MAX_AGE_DAYS", "30")),
                    help="poll mode: leave pull requests not updated in this many days alone until pushed "
                         "(default: COMPAT_REVIEW_POLL_MAX_AGE_DAYS or 30; 0 reviews everything)")
    ap.add_argument("--no-webhook", action="store_true", help="poll only: do not listen for webhooks")
    ap.add_argument("--max-mirrors-gb", type=float, metavar="GB",
                    help="budget for all mirrors together (default: COMPAT_REVIEW_MAX_MIRRORS_GB or 50)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env(args.data, post=not args.no_post, max_mirrors_gb=args.max_mirrors_gb)
    if not settings.model:
        ap.error("COMPAT_REVIEW_MODEL is required")
    if settings.max_mirror_bytes <= 0:
        ap.error("--max-mirrors-gb must be positive")
    with DataLock(settings.data_dir):  # never while another process holds a tree extracted here
        stale = clear_tmp(settings)
    if stale:
        log.info("removed %s extraction director%s left by an earlier run", stale, "y" if stale == 1 else "ies")
    log.info("mirrors use %.1f GB of a %g GB budget", mirrors_size_gb(settings), settings.max_mirror_bytes / 1024**3)
    try:
        repos = parse_repos(args.poll or [os.environ.get("COMPAT_REVIEW_POLL_REPOS", "")])
    except ValueError as e:
        ap.error(str(e))
    if args.no_webhook and not repos:
        ap.error("--no-webhook needs --poll OWNER/REPO (or COMPAT_REVIEW_POLL_REPOS)")
    if args.interval < 1:
        ap.error("--interval must be at least 1 second")

    if args.once:
        repo_part, number = args.once.rsplit("#", 1)
        owner, repo = repo_part.split("/", 1)
        try:
            gh, installation_id = client_for_repo(settings, owner, repo)
            pr = gh.pull(owner, repo, int(number))
            job = Job(owner, repo, int(number), pr["base"]["ref"], pr.get("title") or "", pr.get("body") or "",
                      installation_id=installation_id, draft=bool(pr.get("draft")))
            result = handle(job, settings, gh)
        except (RuntimeError, OSError) as e:
            ap.exit(1, f"{type(e).__name__}: {str(e)[:400]}\n")
        print(json.dumps(result, indent=1) if result else "nothing reviewed")
        return 0

    worker = Worker(settings)
    worker.start()
    log.info("posting %s, data in %s", "on" if settings.post else "off", settings.data_dir)
    poller = None
    if repos:
        poller = Poller(settings, worker, repos, args.interval, max_age_days=args.max_age)
        poller.start()
        log.info("polling %s every %ss, pull requests updated within %g days", ", ".join(f"{o}/{r}" for o, r in repos),
                 args.interval, args.max_age)
    if args.no_webhook:
        try:
            poller.join()
        except KeyboardInterrupt:
            pass
        return 0

    if not settings.webhook_secret:
        log.warning("GITHUB_WEBHOOK_SECRET is not set: signatures are not checked")
    server = ThreadingHTTPServer(("", args.port), make_handler(settings, worker))
    log.info("listening on :%s for webhooks", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
