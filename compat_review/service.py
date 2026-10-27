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

Every review goes into the SQLite database under the data directory, a background thread keeps
the feedback on posted reviews current (reactions, replies, the pull request's fate), and the
HTTP server serves a live dashboard at / with CSV, JSON and Markdown exports.

Configuration comes from settings.toml (and .secrets.toml) found from the working directory up, or
the file given with --config; environment variables with the prefix COMPAT_REVIEW_ override it, and
the flags below override both. `settings.example.toml` documents every key: model, api, effort,
base_url, api_key, headers, timeout, attempts, price_in_per_m, price_out_per_m, data, port,
poll_repos, poll_interval, poll_max_age_days, skip_drafts, max_mirrors_gb, feedback_interval,
dashboard_token, github_token, github_app_id, github_app_private_key(_file), github_webhook_secret.
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
import urllib.parse
from dataclasses import dataclass, field

try:
    import fcntl
except ImportError:  # Windows: no cross-process lock; one process per data directory is on the operator
    fcntl = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import PROMPT_VERSION, RULE_VERSION, config, enrich, feedback, post, report, review, scope
from .change import build_input, diff_from_merge_base, extract_tree, read_config, context_file
from .db import DB, utcnow
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
    price_in: float | None = None
    price_out: float | None = None
    feedback_interval: int = 1800
    dashboard_token: str | None = None
    app_id: str | None = None
    private_key: str | None = None
    webhook_secret: str | None = None
    token: str | None = None
    repo_overrides: dict = field(default_factory=dict)
    db: object = None

    def database(self):
        if self.db is None:
            self.db = DB(self.data_dir / "compat-review.sqlite3")
        return self.db

    @classmethod
    def from_config(cls, cfg, post=True, data_dir=None, max_mirrors_gb=None):
        """Settings from a loaded configuration; `data_dir` and `max_mirrors_gb` are flag overrides."""
        gb = float(max_mirrors_gb if max_mirrors_gb is not None else cfg.MAX_MIRRORS_GB)
        return cls(data_dir=Path(data_dir or cfg.DATA), model=cfg.MODEL or "", api=cfg.API, effort=cfg.EFFORT,
                   base_url=cfg.BASE_URL or None, post=post, skip_drafts=bool(cfg.SKIP_DRAFTS),
                   max_mirror_bytes=int(gb * 1024**3),
                   price_in=config.price(cfg, "PRICE_IN_PER_M"), price_out=config.price(cfg, "PRICE_OUT_PER_M"),
                   feedback_interval=int(cfg.FEEDBACK_INTERVAL), dashboard_token=cfg.DASHBOARD_TOKEN or None,
                   app_id=str(cfg.GITHUB_APP_ID) if cfg.GITHUB_APP_ID else None, private_key=config.private_key(cfg),
                   webhook_secret=cfg.GITHUB_WEBHOOK_SECRET or None, token=cfg.GITHUB_TOKEN or None,
                   repo_overrides=config.repo_overrides(cfg))


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
    return settings.database().reviewed_heads(owner, repo, posted_only=settings.post)


def publish(gh, job, result, db, review_id):
    """The note into the PR description and the gradable comment; the record notes the comment."""
    result["_meta"]["posted_at"] = utcnow()
    pr = gh.pull(job.owner, job.repo, job.number)
    gh.update_pull_body(job.owner, job.repo, job.number, post.splice(pr.get("body"), post.note(result)))
    c = gh.create_comment(job.owner, job.repo, job.number, post.comment(result))
    result["_meta"]["comment_id"] = c.get("id")
    db.mark_posted(review_id, c.get("id"), result["_meta"]["posted_at"])


def recover(db, gh, job, diff_sha, settings):
    """With no database row for this diff, look for the bot's own record on GitHub: the note in the
    description first (already in hand), then the bot's comments. Stores and returns the row, or None.
    This is what keeps a diff from being reviewed twice when the database is gone."""
    rec = post.parse_note_record(job.body)
    if rec and rec.get("diff_sha256") == diff_sha:
        db.insert_recovered(job.owner, job.repo, job.number, rec, title=job.title, posted_text=post.posted_lines(job.body),
                            price_in=settings.price_in, price_out=settings.price_out)
        log.info("%s/%s#%s: diff %s recovered from the note in the description", job.owner, job.repo, job.number, diff_sha[:12])
        return db.find_review(job.owner, job.repo, job.number, diff_sha)
    try:
        comments = gh.comments(job.owner, job.repo, job.number)
    except (GitHubError, OSError) as e:
        log.warning("%s/%s#%s: could not read comments to look for an earlier review: %s",
                    job.owner, job.repo, job.number, str(e)[:200])
        return None
    for c in comments:
        rec = post.parse_record(c.get("body"))
        if rec and rec.get("diff_sha256") == diff_sha:
            db.insert_recovered(job.owner, job.repo, job.number, rec, comment_id=c.get("id"), posted_at=c.get("created_at"),
                                title=job.title, posted_text=post.posted_lines(c.get("body")),
                                price_in=settings.price_in, price_out=settings.price_out)
            log.info("%s/%s#%s: diff %s recovered from the bot's comment", job.owner, job.repo, job.number, diff_sha[:12])
            return db.find_review(job.owner, job.repo, job.number, diff_sha)
    return None


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
    db = settings.database()
    stored = db.find_review(job.owner, job.repo, job.number, diff_sha) or recover(db, gh, job, diff_sha, settings)
    if stored:
        if settings.post and stored["posted_at"] is None:
            result = json.loads(stored["result_json"])
            publish(gh, job, result, db, stored["id"])
            log.info("%s/%s#%s: diff %s reviewed earlier, posted now", job.owner, job.repo, job.number, diff_sha[:12])
            return result
        log.info("%s/%s#%s: diff %s already reviewed", job.owner, job.repo, job.number, diff_sha[:12])
        return None

    # the repository's own .compatibility-review.toml at the merge base; the service's per-repository
    # override stands in for each key it lacks
    cfg = {**settings.repo_overrides.get(f"{job.owner}/{job.repo}", {}), **tomllib.loads(read_config(mirror, merge_base))}
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
            if cfg.get("context_file") == settings.repo_overrides.get(f"{job.owner}/{job.repo}", {}).get("context_file"):
                context = Path(cfg["context_file"]).read_text()  # the service's file, not the repository's
            else:
                context = context_file(mirror, merge_base, cfg["context_file"])
        else:
            context = (Path(__file__).resolve().parent / "context.md").read_text()
        result = reviewer(user, context, settings.model, settings.api, settings.base_url, settings.effort)
    result["_meta"].update({"owner": job.owner, "repo": job.repo, "number": job.number, "base": merge_base,
                            "head": head_sha, "diff_sha256": diff_sha, "reviewed_at": utcnow()})
    # stored first: a failed post never costs a second model call
    review_id = db.insert_review(result, title=job.title, base_ref=job.base_ref,
                                 price_in=settings.price_in, price_out=settings.price_out)
    if settings.post:
        publish(gh, job, result, db, review_id)
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

    def __init__(self, settings, worker, repos, interval, client=client_for_repo, max_age_days=7):
        super().__init__(daemon=True)
        self.settings, self.worker, self.repos, self.interval, self.client = settings, worker, repos, interval, client
        self.max_age_days = max_age_days
        self.seen = {}  # (owner, repo, number) -> head sha queued or already reviewed
        self.loaded = set()
        self.tallied = set()  # repositories whose first scan has been logged in full
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
            first = (owner, repo) not in self.tallied
            stale, recovered, remembered, drafts = [], [], 0, 0
            db = self.settings.database()
            for pr in pulls:
                key, head = (owner, repo, pr["number"]), pr["head"]["sha"]
                if self.seen.get(key) == head:
                    remembered += 1
                    continue
                if pr.get("draft") and self.settings.skip_drafts:
                    drafts += 1
                    continue  # not remembered: reviewed once it is ready
                self.seen[key] = head
                rec = post.parse_note_record(pr.get("body"))
                if rec and rec.get("head") == head and rec.get("diff_sha256"):
                    # the note in the description says this head was reviewed: nothing to fetch or call
                    if not db.find_review(owner, repo, pr["number"], rec["diff_sha256"]):
                        db.insert_recovered(owner, repo, pr["number"], rec, title=pr.get("title"),
                                            posted_text=post.posted_lines(pr.get("body")),
                                            price_in=self.settings.price_in, price_out=self.settings.price_out)
                        recovered.append(pr["number"])
                    continue
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
            if recovered:
                log.info("%s/%s: %s review%s already on the pull requests, recorded without a fetch", owner, repo,
                         len(recovered), "" if len(recovered) == 1 else "s")
            if first:  # the first scan says what it made of every open pull request; later ones log only changes
                mine = [j.number for j in queued if (j.owner, j.repo) == (owner, repo)]
                log.info("%s/%s: %s open pull request%s: %s at a reviewed head, %s recorded from notes, %s draft%s, "
                         "%s stale, %s queued", owner, repo, len(pulls), "" if len(pulls) == 1 else "s", remembered,
                         len(recovered), drafts, "" if drafts == 1 else "s", len(stale), len(mine))
                self.tallied.add((owner, repo))
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


class FeedbackPoller(threading.Thread):
    """Keeps the feedback on posted reviews current: every interval for a week after posting, then every
    six hours for two months, then weekly. Three conditional GETs per review, free when unchanged."""

    def __init__(self, settings, interval, client=client_for_repo):
        super().__init__(daemon=True)
        self.settings, self.interval, self.client = settings, interval, client
        self.stop = threading.Event()

    def refresh_due(self):
        """One pass. Returns (refreshed, failed)."""
        db = self.settings.database()
        clients, ok, failed = {}, 0, 0
        for row in db.due_for_feedback(self.interval):
            key = (row["owner"], row["repo"])
            try:
                if key not in clients:
                    clients[key] = self.client(self.settings, *key)[0]
                fields = feedback.refresh(clients[key], row)
                if fields.get("resolved_comment_id"):  # a review recovered from a note now knows its comment
                    db.mark_posted(row["id"], fields["resolved_comment_id"], fields.get("resolved_posted_at"))
                db.upsert_feedback(row["id"], fields)
                ok += 1
            except (GitHubError, RuntimeError, OSError) as e:
                failed += 1
                log.error("%s/%s#%s: feedback refresh failed: %s", row["owner"], row["repo"], row["number"], str(e)[:300])
        return ok, failed

    def run(self):
        while not self.stop.is_set():
            try:
                ok, failed = self.refresh_due()
                if ok or failed:
                    log.info("feedback: refreshed %s review%s%s", ok, "" if ok == 1 else "s",
                             f", {failed} failed" if failed else "")
            except Exception:  # noqa: BLE001 - keep going
                log.exception("feedback refresh failed")
            self.stop.wait(self.interval)


def rebuild_from_notes(settings, owner, repo, days, client=client_for_repo):
    """Reviews recorded in the notes on pull requests updated in the last `days` days, into the database.

    One request per hundred pull requests, newest first, and no model call: the record in each note
    carries the verdict, the versions and the token usage. This is how the metrics survive a restart
    that loses the database. Returns how many reviews were new to the database.
    """
    from datetime import datetime, timedelta, timezone

    db = settings.database()
    gh, _ = client(settings, owner, repo)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n, page = 0, 1
    while True:
        pulls = gh.request("GET", f"/repos/{owner}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}")
        for pr in pulls:
            if (pr.get("updated_at") or "") < cutoff:
                return n
            rec = post.parse_note_record(pr.get("body"))
            if rec and rec.get("diff_sha256") and not db.find_review(owner, repo, pr["number"], rec["diff_sha256"]):
                db.insert_recovered(owner, repo, pr["number"], rec, title=pr.get("title"),
                                    posted_text=post.posted_lines(pr.get("body")),
                                    price_in=settings.price_in, price_out=settings.price_out)
                n += 1
        if len(pulls) < 100:
            return n
        page += 1


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


def make_handler(settings, worker, webhook=True):
    dashboard = Path(__file__).resolve().parent / "dashboard.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _reply(self, code, text, content_type="text/plain; charset=utf-8", filename=None):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, params):
            if not settings.dashboard_token:
                return True
            given = (params.get("token") or [None])[0]
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                given = given or auth[7:]
            return bool(given) and hmac.compare_digest(given, settings.dashboard_token)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            params = urllib.parse.parse_qs(query)
            if path == "/healthz":
                return self._reply(200, "ok\n")
            if path not in ("/", "/api/summary", "/api/reviews", "/export.csv", "/export.json", "/report.md"):
                return self._reply(404, "not found\n")
            if not self._authorized(params):
                return self._reply(401, "a dashboard token is required\n")
            if path == "/":
                return self._reply(200, dashboard.read_text(), "text/html; charset=utf-8")
            since = (params.get("since") or [None])[0]
            since = None if since in (None, "", "all") else float(since)
            repo = (params.get("repo") or [None])[0] or None
            db = settings.database()
            if path == "/api/summary":
                return self._reply(200, json.dumps(report.summarize(db.rows(since, repo))), "application/json")
            if path == "/api/reviews":
                limit = int((params.get("limit") or ["100"])[0])
                return self._reply(200, json.dumps(report.grade_rows(db.rows(since, repo, limit))), "application/json")
            stamp = utcnow()[:10]
            if path == "/export.csv":
                return self._reply(200, report.render_csv(db.rows(since, repo)), "text/csv; charset=utf-8",
                                   f"compatibility-review-{stamp}.csv")
            if path == "/export.json":
                return self._reply(200, report.render_json(db.rows(since, repo)), "application/json",
                                   f"compatibility-review-{stamp}.json")
            window = f"last {since:g} days" if since else "all time"
            return self._reply(200, report.render_md(report.summarize(db.rows(since, repo)), window=window, repo=repo),
                               "text/markdown; charset=utf-8", f"compatibility-review-{stamp}.md")

        def do_POST(self):
            if self.path != "/webhook" or not webhook:
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
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    try:
        cfg = config.load(pre.parse_known_args(argv)[0].config)
    except (OSError, ValueError) as e:
        sys.exit(f"configuration: {e}")
    ap = argparse.ArgumentParser(prog="compatibility-review-service", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", metavar="FILE", help="settings file (default: settings.toml, found from the working directory up)")
    ap.add_argument("--port", type=int, default=cfg.PORT, help="(default: the port setting, 8000)")
    ap.add_argument("--data", help="data directory (default: the data setting, ./data)")
    ap.add_argument("--no-post", action="store_true", help="review and store results, write nothing to GitHub")
    ap.add_argument("--once", metavar="OWNER/REPO#N", help="review one pull request now and exit; no server")
    ap.add_argument("--poll", metavar="OWNER/REPO", action="append",
                    help="poll this repository's open pull requests (repeat or comma-separate; "
                         "default: the poll_repos setting)")
    ap.add_argument("--interval", type=int, default=cfg.POLL_INTERVAL,
                    help="seconds between polls (default: the poll_interval setting, 60)")
    ap.add_argument("--max-age", type=float, metavar="DAYS", default=cfg.POLL_MAX_AGE_DAYS,
                    help="poll mode: leave pull requests not updated in this many days alone until pushed "
                         "(default: the poll_max_age_days setting, 7; 0 reviews everything)")
    ap.add_argument("--no-webhook", action="store_true", help="poll only: POST /webhook is not accepted")
    ap.add_argument("--no-feedback", action="store_true", help="do not refresh feedback on posted reviews")
    ap.add_argument("--max-mirrors-gb", type=float, metavar="GB",
                    help="budget for all mirrors together (default: the max_mirrors_gb setting, 50)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_config(cfg, post=not args.no_post, data_dir=args.data, max_mirrors_gb=args.max_mirrors_gb)
    if not settings.model:
        ap.error("a model is required: `model` in settings.toml or COMPAT_REVIEW_MODEL")
    if settings.max_mirror_bytes <= 0:
        ap.error("--max-mirrors-gb must be positive")
    with DataLock(settings.data_dir):  # never while another process holds a tree extracted here
        stale = clear_tmp(settings)
        imported = settings.database().import_json(settings.data_dir / "reviews", settings.price_in, settings.price_out)
    if stale:
        log.info("removed %s extraction director%s left by an earlier run", stale, "y" if stale == 1 else "ies")
    if imported:
        log.info("imported %s review%s from JSON files into the database", imported, "" if imported == 1 else "s")
    log.info("mirrors use %.1f GB of a %g GB budget", mirrors_size_gb(settings), settings.max_mirror_bytes / 1024**3)
    if settings.price_in is None or settings.price_out is None:
        log.info("no token prices configured (price_in_per_m / price_out_per_m): costs will show as n/a")
    try:
        repos = parse_repos(args.poll or config.repos(cfg))
    except ValueError as e:
        ap.error(str(e))
    if args.no_webhook and not repos:
        ap.error("--no-webhook needs --poll OWNER/REPO (or the poll_repos setting)")
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

    if cfg.REBUILD_DAYS and repos:
        for owner, repo in repos:
            try:
                n = rebuild_from_notes(settings, owner, repo, cfg.REBUILD_DAYS)
                if n:
                    log.info("%s/%s: %s review%s rebuilt from notes on pull requests updated in the last %g days",
                             owner, repo, n, "" if n == 1 else "s", cfg.REBUILD_DAYS)
            except (GitHubError, RuntimeError, OSError) as e:
                log.warning("%s/%s: rebuild from notes failed: %s", owner, repo, str(e)[:300])
    worker = Worker(settings)
    worker.start()
    log.info("posting %s, data in %s", "on" if settings.post else "off", settings.data_dir)
    if repos:
        Poller(settings, worker, repos, args.interval, max_age_days=args.max_age).start()
        log.info("polling %s every %ss, pull requests updated within %g days", ", ".join(f"{o}/{r}" for o, r in repos),
                 args.interval, args.max_age)
    if not args.no_feedback:
        FeedbackPoller(settings, settings.feedback_interval).start()
    if not args.no_webhook and not settings.webhook_secret:
        log.warning("GITHUB_WEBHOOK_SECRET is not set: signatures are not checked")
    server = ThreadingHTTPServer(("", args.port), make_handler(settings, worker, webhook=not args.no_webhook))
    log.info("dashboard at http://localhost:%s/%s%s", args.port, "?token=..." if settings.dashboard_token else "",
             "" if args.no_webhook else ", webhooks at /webhook")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
