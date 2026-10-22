"""The service's record: every review and the feedback on it, in one SQLite file under the data directory.

Each thread opens its own connection per call; the file is in WAL mode, so readers (the dashboard)
never block the writer (the worker). Older JSON result files are imported once at startup.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL, diff_sha256 TEXT NOT NULL,
    head TEXT, base TEXT, base_ref TEXT, title TEXT,
    verdict TEXT, color TEXT, mechanism TEXT, prevalence TEXT, auto_preserved TEXT, widens TEXT,
    breaking_setup TEXT, summary TEXT, skipped INTEGER NOT NULL DEFAULT 0,
    prompt_version TEXT, rule_version TEXT, model TEXT, api TEXT, effort TEXT, enriched INTEGER,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    tokens_reasoning INTEGER NOT NULL DEFAULT 0, latency_s REAL,
    price_in REAL, price_out REAL, cost_usd REAL,
    reviewed_at TEXT NOT NULL, posted_at TEXT, comment_id INTEGER,
    result_json TEXT NOT NULL,
    UNIQUE (owner, repo, number, diff_sha256)
);
CREATE INDEX IF NOT EXISTS reviews_by_repo ON reviews (owner, repo, reviewed_at);
CREATE TABLE IF NOT EXISTS feedback (
    review_id INTEGER PRIMARY KEY REFERENCES reviews (id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    thumbs_up INTEGER NOT NULL DEFAULT 0, thumbs_down INTEGER NOT NULL DEFAULT 0,
    other_reactions INTEGER NOT NULL DEFAULT 0, reactors TEXT NOT NULL DEFAULT '[]',
    replies TEXT NOT NULL DEFAULT '[]', reply_count INTEGER NOT NULL DEFAULT 0,
    comment_deleted INTEGER NOT NULL DEFAULT 0, note_present INTEGER,
    pr_state TEXT, merged INTEGER, merged_at TEXT, closed_at TEXT,
    etag_reactions TEXT, etag_comments TEXT, etag_pull TEXT
);
"""

REVIEW_COLUMNS = [
    "owner", "repo", "number", "diff_sha256", "head", "base", "base_ref", "title",
    "verdict", "color", "mechanism", "prevalence", "auto_preserved", "widens", "breaking_setup", "summary", "skipped",
    "prompt_version", "rule_version", "model", "api", "effort", "enriched",
    "tokens_in", "tokens_out", "tokens_reasoning", "latency_s", "price_in", "price_out", "cost_usd",
    "reviewed_at", "posted_at", "comment_id", "result_json",
]
FEEDBACK_COLUMNS = [
    "fetched_at", "thumbs_up", "thumbs_down", "other_reactions", "reactors", "replies", "reply_count",
    "comment_deleted", "note_present", "pr_state", "merged", "merged_at", "closed_at",
    "etag_reactions", "etag_comments", "etag_pull",
]


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(text):
    """A stored or GitHub timestamp as an aware datetime; None for none."""
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cost_usd(tokens_in, tokens_out, price_in, price_out):
    """Dollars at prices per million tokens; None when a price is unknown."""
    if price_in is None or price_out is None:
        return None
    return (tokens_in * price_in + tokens_out * price_out) / 1e6


def review_fields(result, title=None, base_ref=None, price_in=None, price_out=None, reviewed_at=None):
    """A reviews row from a result dict whose _meta names owner, repo, number, diff_sha256."""
    m = result.get("_meta") or {}
    usage = m.get("usage") or {}
    tokens_in, tokens_out = int(usage.get("in") or 0), int(usage.get("out") or 0)
    return {
        "owner": m["owner"], "repo": m["repo"], "number": int(m["number"]), "diff_sha256": m["diff_sha256"],
        "head": m.get("head"), "base": m.get("base"), "base_ref": base_ref, "title": title,
        "verdict": result.get("verdict"), "color": result.get("color"), "mechanism": result.get("q_mechanism"),
        "prevalence": result.get("q_prevalence"), "auto_preserved": result.get("q_auto_preserved"),
        "widens": result.get("q_widens_access_or_changes_output"), "breaking_setup": result.get("q_breaking_setup"),
        "summary": result.get("summary"), "skipped": 1 if m.get("skipped") else 0,
        "prompt_version": m.get("prompt"), "rule_version": m.get("rule"), "model": m.get("model"),
        "api": m.get("api"), "effort": m.get("effort"),
        "enriched": None if m.get("enriched") is None else int(bool(m.get("enriched"))),
        "tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_reasoning": int(usage.get("reasoning") or 0),
        "latency_s": m.get("latency_s"), "price_in": price_in, "price_out": price_out,
        "cost_usd": cost_usd(tokens_in, tokens_out, price_in, price_out),
        "reviewed_at": reviewed_at or m.get("reviewed_at") or utcnow(),
        "posted_at": m.get("posted_at"), "comment_id": m.get("comment_id"),
        "result_json": json.dumps(result, separators=(",", ":")),
    }


class DB:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self.connect()
        try:
            c.executescript(SCHEMA)
        finally:
            c.close()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _all(self, sql, params=()):
        c = self.connect()
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        finally:
            c.close()

    def _one(self, sql, params=()):
        rows = self._all(sql + " LIMIT 1", params)
        return rows[0] if rows else None

    def _exec(self, sql, params=()):
        c = self.connect()
        try:
            return c.execute(sql, params).lastrowid
        finally:
            c.close()

    # reviews

    def insert_review(self, result, **kw):
        """Store a result; a second store of the same diff updates its posting state. Returns the row id."""
        f = review_fields(result, **kw)
        cols = ", ".join(REVIEW_COLUMNS)
        marks = ", ".join("?" for _ in REVIEW_COLUMNS)
        self._exec(f"INSERT INTO reviews ({cols}) VALUES ({marks}) ON CONFLICT (owner, repo, number, diff_sha256) "
                   "DO UPDATE SET posted_at = COALESCE(excluded.posted_at, reviews.posted_at), "
                   "comment_id = COALESCE(excluded.comment_id, reviews.comment_id), result_json = excluded.result_json",
                   [f[c] for c in REVIEW_COLUMNS])
        return self.find_review(f["owner"], f["repo"], f["number"], f["diff_sha256"])["id"]

    def find_review(self, owner, repo, number, diff_sha256):
        return self._one("SELECT * FROM reviews WHERE owner = ? AND repo = ? AND number = ? AND diff_sha256 = ?",
                         (owner, repo, number, diff_sha256))

    def mark_posted(self, review_id, comment_id, posted_at=None):
        self._exec("UPDATE reviews SET comment_id = ?, posted_at = ? WHERE id = ?",
                   (comment_id, posted_at or utcnow(), review_id))

    def reviewed_heads(self, owner, repo, posted_only=False):
        """{number: head} of this repository's stored reviews, latest review of a number winning."""
        sql = "SELECT number, head FROM reviews WHERE owner = ? AND repo = ? AND head IS NOT NULL"
        if posted_only:
            sql += " AND comment_id IS NOT NULL"
        return {r["number"]: r["head"] for r in self._all(sql + " ORDER BY reviewed_at, id", (owner, repo))}

    def repos(self):
        return [f"{r['owner']}/{r['repo']}" for r in self._all("SELECT DISTINCT owner, repo FROM reviews ORDER BY 1, 2")]

    def rows(self, since_days=None, repo=None, limit=None):
        """Reviews joined with their feedback, newest first, without the full result JSON."""
        review_cols = ", ".join(f"r.{c}" for c in REVIEW_COLUMNS if c != "result_json")
        feedback_cols = ", ".join(f"f.{c} AS {'feedback_fetched_at' if c == 'fetched_at' else c}" for c in FEEDBACK_COLUMNS)
        sql = f"SELECT r.id, {review_cols}, {feedback_cols} FROM reviews r LEFT JOIN feedback f ON f.review_id = r.id WHERE 1 = 1"
        params = []
        if since_days is not None:
            cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
            sql += " AND r.reviewed_at >= ?"
            params.append(datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds"))
        if repo:
            sql += " AND r.owner || '/' || r.repo = ?"
            params.append(repo)
        sql += " ORDER BY r.reviewed_at DESC, r.id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self._all(sql, params)

    def result(self, review_id):
        row = self._one("SELECT result_json FROM reviews WHERE id = ?", (review_id,))
        return json.loads(row["result_json"]) if row else None

    # feedback

    def due_for_feedback(self, interval_s, now=None):
        """Posted reviews whose feedback is due a refresh: every interval for a week after posting,
        every six hours for two months, weekly after that."""
        now = now or datetime.now(timezone.utc)
        due = []
        for r in self._all("SELECT r.id, r.owner, r.repo, r.number, r.comment_id, r.posted_at, r.verdict, "
                           + ", ".join(f"f.{c} AS {'feedback_fetched_at' if c == 'fetched_at' else c}" for c in FEEDBACK_COLUMNS)
                           + " FROM reviews r LEFT JOIN feedback f ON f.review_id = r.id WHERE r.comment_id IS NOT NULL"):
            posted = parse_ts(r["posted_at"]) or now
            age_days = (now - posted).total_seconds() / 86400
            every = interval_s if age_days < 7 else 6 * 3600 if age_days < 60 else 7 * 86400
            fetched = parse_ts(r["feedback_fetched_at"])
            if fetched is None or (now - fetched).total_seconds() >= every:
                due.append(r)
        return due

    def upsert_feedback(self, review_id, fields):
        f = {c: fields.get(c) for c in FEEDBACK_COLUMNS}
        f["fetched_at"] = f["fetched_at"] or utcnow()
        for c in ("reactors", "replies"):
            if not isinstance(f[c], str):
                f[c] = json.dumps(f[c] or [])
        for c in ("thumbs_up", "thumbs_down", "other_reactions", "reply_count", "comment_deleted"):
            f[c] = int(f[c] or 0)
        cols = ", ".join(["review_id", *FEEDBACK_COLUMNS])
        marks = ", ".join("?" for _ in range(len(FEEDBACK_COLUMNS) + 1))
        self._exec(f"INSERT OR REPLACE INTO feedback ({cols}) VALUES ({marks})", [review_id, *(f[c] for c in FEEDBACK_COLUMNS)])

    # legacy

    def import_json(self, reviews_root, price_in=None, price_out=None):
        """Bring result files written before the database existed into it. Returns how many were new."""
        root = Path(reviews_root)
        if not root.is_dir():
            return 0
        n = 0
        for path in sorted(root.glob("*/*.json")):
            try:
                result = json.loads(path.read_text())
                m = result["_meta"]
                if not all(k in m for k in ("owner", "repo", "number", "diff_sha256")):
                    continue
                if self.find_review(m["owner"], m["repo"], m["number"], m["diff_sha256"]):
                    continue
                when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
                self.insert_review(result, reviewed_at=m.get("reviewed_at") or when, price_in=price_in, price_out=price_out)
                n += 1
            except (ValueError, KeyError, OSError):
                continue
        return n
