"""What maintainers did with a posted review: reactions on the comment, replies after it, whether the
comment or the note survived, and what became of the pull request.

Three conditional GETs per refresh. GitHub answers an unchanged resource with 304 and does not count
it against the rate limit, so refreshing often is cheap.
"""

import re
from datetime import timedelta

from .db import parse_ts, utcnow
from .github import GitHubError
from .post import BEGIN, parse_record

MENTION = re.compile(r"compatib|verdict|upgrade compatibility", re.I)
EXCERPT = 300


def _resolve_comment(gh, row, out):
    """For a review recovered from the note in a PR description: find the bot's comment for the same diff.
    Returns the row with the comment known, or None when there is no such comment."""
    owner, repo, number = row["owner"], row["repo"], row["number"]
    for c in gh.comments(owner, repo, number):
        rec = parse_record(c.get("body"))
        if rec and rec.get("diff_sha256") == row.get("diff_sha256"):
            out["resolved_comment_id"], out["resolved_posted_at"] = c["id"], c.get("created_at")
            return {**row, "comment_id": c["id"], "posted_at": c.get("created_at") or row.get("posted_at")}
    out.update({"comment_deleted": 1, "thumbs_up": 0, "thumbs_down": 0, "other_reactions": 0, "reactors": []})
    return None


def _reactions(gh, row, out):
    owner, repo, cid = row["owner"], row["repo"], row["comment_id"]
    try:
        data, etag = gh.get_if_changed(f"/repos/{owner}/{repo}/issues/comments/{cid}/reactions?per_page=100",
                                       row.get("etag_reactions"))
    except GitHubError as e:
        if e.status == 404:
            out.update({"comment_deleted": 1, "thumbs_up": 0, "thumbs_down": 0, "other_reactions": 0, "reactors": []})
            return
        raise
    out["etag_reactions"] = etag
    if data is None:  # unchanged
        return
    counts = {"+1": 0, "-1": 0}
    reactors = []
    for r in data:
        content = r.get("content")
        login = (r.get("user") or {}).get("login")
        if content in counts:
            counts[content] += 1
        reactors.append({"login": login, "content": content})
    out.update({"thumbs_up": counts["+1"], "thumbs_down": counts["-1"],
                "other_reactions": len(data) - counts["+1"] - counts["-1"], "reactors": reactors})


def _replies(gh, row, out):
    owner, repo, number, cid = row["owner"], row["repo"], row["number"], row["comment_id"]
    posted = parse_ts(row.get("posted_at"))
    since = (posted - timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z") if posted else None
    path = f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100" + (f"&since={since}" if since else "")
    data, etag = gh.get_if_changed(path, row.get("etag_comments"))
    out["etag_comments"] = etag
    if data is None:
        return
    ours = next((c for c in data if c.get("id") == cid), None)
    if ours is None and since:
        out["comment_deleted"] = 1
    after = (ours or {}).get("created_at") or row.get("posted_at") or ""
    replies = []
    for c in data:
        user = c.get("user") or {}
        if c.get("id") == cid or user.get("type") == "Bot" or (c.get("created_at") or "") <= after:
            continue
        body = c.get("body") or ""
        replies.append({"login": user.get("login"), "created_at": c.get("created_at"), "url": c.get("html_url"),
                        "excerpt": body[:EXCERPT], "mentions_review": bool(MENTION.search(body))})
    out.update({"replies": replies, "reply_count": len(replies)})


def _pull(gh, row, out):
    owner, repo, number = row["owner"], row["repo"], row["number"]
    data, etag = gh.get_if_changed(f"/repos/{owner}/{repo}/pulls/{number}", row.get("etag_pull"))
    out["etag_pull"] = etag
    if data is None:
        return
    out.update({"pr_state": data.get("state"), "merged": int(bool(data.get("merged") or data.get("merged_at"))),
                "merged_at": data.get("merged_at"), "closed_at": data.get("closed_at"),
                "note_present": int(BEGIN in (data.get("body") or ""))})


def refresh(gh, row):
    """The feedback fields for one posted review, starting from what was stored (`row`) and changing only
    what GitHub reports as changed."""
    out = {c: row.get(c) for c in ("thumbs_up", "thumbs_down", "other_reactions", "reactors", "replies", "reply_count",
                                   "comment_deleted", "note_present", "pr_state", "merged", "merged_at", "closed_at",
                                   "etag_reactions", "etag_comments", "etag_pull")}
    out["fetched_at"] = utcnow()
    pull_row = {k: row.get(k) for k in ("owner", "repo", "number", "etag_pull")}
    if not row.get("comment_id"):
        row = _resolve_comment(gh, row, out)
    if row is not None:
        _reactions(gh, row, out)
        _replies(gh, row, out)
    _pull(gh, pull_row, out)
    return out
