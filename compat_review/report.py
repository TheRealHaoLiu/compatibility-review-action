"""What the service spent and what maintainers said: a summary and a grade sheet from the database.

    compatibility-review-report [--data ./data] [--since DAYS] [--repo OWNER/REPO] [--format md|csv|json] [--out FILE]

The dashboard's /api/summary, /export.csv, /export.json and /report.md serve the same things live.
"""

import argparse
import csv
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .db import DB

COLUMNS = [
    "repo", "number", "url", "title", "verdict", "color", "mechanism", "prevalence", "auto_preserved", "widens",
    "skipped", "model", "prompt_version", "rule_version", "effort", "enriched", "reviewed_at", "head", "base",
    "tokens_in", "tokens_out", "tokens_reasoning", "latency_s", "cost_usd", "posted_at", "comment_id",
    "thumbs_up", "thumbs_down", "reactors", "reply_count", "first_reply", "comment_deleted", "note_present",
    "pr_state", "merged", "feedback_fetched_at",
]


def grade_rows(rows):
    """One flat dict per review for a sheet: repository and PR link first, then the verdict, then the feedback."""
    out = []
    for r in rows:
        reactors = json.loads(r.get("reactors") or "[]") if isinstance(r.get("reactors"), str) else (r.get("reactors") or [])
        replies = json.loads(r.get("replies") or "[]") if isinstance(r.get("replies"), str) else (r.get("replies") or [])
        g = {c: r.get(c) for c in COLUMNS}
        g["repo"] = f"{r['owner']}/{r['repo']}"
        g["url"] = f"https://github.com/{r['owner']}/{r['repo']}/pull/{r['number']}"
        g["reactors"] = " ".join(f"{x.get('login')}:{x.get('content')}" for x in reactors)
        g["first_reply"] = (replies[0].get("excerpt") or "").replace("\n", " ") if replies else ""
        g["replies"] = replies
        g["id"] = r.get("id")
        out.append(g)
    return out


def _percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(p * (len(values) - 1))))
    return values[k]


def summarize(rows):
    """Totals, verdicts, per-repository and per-version tables, feedback tallies, and what needs a look."""
    modeled = [r for r in rows if not r.get("skipped")]
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    latencies = [r["latency_s"] for r in modeled if r.get("latency_s") is not None]
    verdicts = Counter(r.get("verdict") for r in rows)

    by_repo = defaultdict(lambda: Counter())
    for r in rows:
        c = by_repo[f"{r['owner']}/{r['repo']}"]
        c["reviews"] += 1
        c[r.get("verdict") or "?"] += 1
        c["tokens_in"] += r.get("tokens_in") or 0
        c["tokens_out"] += r.get("tokens_out") or 0
        if r.get("cost_usd") is not None:
            c["cost_usd"] += r["cost_usd"]
        if r.get("comment_id"):
            c["posted"] += 1
        c["thumbs_up"] += r.get("thumbs_up") or 0
        c["thumbs_down"] += r.get("thumbs_down") or 0

    by_version = defaultdict(lambda: Counter())
    version_latency = defaultdict(list)
    for r in modeled:
        key = f"prompt {r.get('prompt_version')} · rule {r.get('rule_version')} · {r.get('model')}"
        c = by_version[key]
        c["reviews"] += 1
        c[r.get("verdict") or "?"] += 1
        if r.get("cost_usd") is not None:
            c["cost_usd"] += r["cost_usd"]
        if r.get("latency_s") is not None:
            version_latency[key].append(r["latency_s"])

    posted = [r for r in rows if r.get("comment_id")]
    fetched = [r for r in posted if r.get("feedback_fetched_at")]
    by_color = {}
    for r in fetched:
        c = by_color.setdefault(r.get("verdict"), {"up": 0, "down": 0, "reviews": 0})
        c["up"] += r.get("thumbs_up") or 0
        c["down"] += r.get("thumbs_down") or 0
        c["reviews"] += 1
    needs_look = [r for r in fetched
                  if (r.get("thumbs_down") or 0) or (r.get("reply_count") or 0) or r.get("comment_deleted")]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reviews": len(rows), "modeled": len(modeled), "skipped": len(rows) - len(modeled),
        "repos": sorted(by_repo),
        "tokens_in": sum(r.get("tokens_in") or 0 for r in rows),
        "tokens_out": sum(r.get("tokens_out") or 0 for r in rows),
        "tokens_reasoning": sum(r.get("tokens_reasoning") or 0 for r in rows),
        "cost_usd": sum(costs) if costs else None, "cost_known": len(costs),
        "latency_median_s": _percentile(latencies, 0.5), "latency_p95_s": _percentile(latencies, 0.95),
        "verdicts": {"breaking": verdicts.get("breaking", 0), "notable": verdicts.get("notable", 0),
                     "none": verdicts.get("none", 0)},
        "by_repo": [{"repo": k, **{kk: (round(v, 4) if kk == "cost_usd" else v) for kk, v in c.items()}}
                    for k, c in sorted(by_repo.items())],
        "by_version": [{"version": k, **{kk: (round(v, 4) if kk == "cost_usd" else v) for kk, v in c.items()},
                        "latency_median_s": _percentile(version_latency[k], 0.5)}
                       for k, c in sorted(by_version.items())],
        "feedback": {
            "posted": len(posted), "fetched": len(fetched),
            "thumbs_up": sum(r.get("thumbs_up") or 0 for r in fetched),
            "thumbs_down": sum(r.get("thumbs_down") or 0 for r in fetched),
            "with_reactions": sum(1 for r in fetched if (r.get("thumbs_up") or 0) + (r.get("thumbs_down") or 0)),
            "replies": sum(r.get("reply_count") or 0 for r in fetched),
            "with_replies": sum(1 for r in fetched if r.get("reply_count")),
            "comment_deleted": sum(1 for r in fetched if r.get("comment_deleted")),
            "note_removed": sum(1 for r in fetched if r.get("note_present") == 0),
            "by_color": by_color,
            "pr_merged": sum(1 for r in fetched if r.get("merged")),
            "pr_closed": sum(1 for r in fetched if r.get("pr_state") == "closed" and not r.get("merged")),
            "pr_open": sum(1 for r in fetched if r.get("pr_state") == "open"),
            "red_merged": sum(1 for r in fetched if r.get("merged") and r.get("verdict") == "breaking"),
        },
        "needs_look": grade_rows(needs_look),
    }


def _money(x):
    return "n/a" if x is None else f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def _seconds(x):
    return "n/a" if x is None else f"{x:.1f} s"


def render_md(summary, title="Compatibility review", window="all time", repo=None):
    s, f = summary, summary["feedback"]
    v = s["verdicts"]
    lines = [f"# {title}", "",
             f"{window}{' · ' + repo if repo else ''} · generated {s['generated_at']}", "",
             "## Spend", "",
             "| | |", "|---|---|",
             f"| reviews | {s['reviews']} ({s['modeled']} by the model, {s['skipped']} skipped by ignore rules) |",
             f"| repositories | {len(s['repos'])} |",
             f"| tokens | {s['tokens_in']:,} in / {s['tokens_out']:,} out ({s['tokens_reasoning']:,} reasoning) |",
             f"| cost | {_money(s['cost_usd'])}" + (f" over {s['cost_known']} priced reviews |" if s["cost_known"] else " (no prices configured) |"),
             f"| latency | median {_seconds(s['latency_median_s'])}, p95 {_seconds(s['latency_p95_s'])} |",
             "",
             "## Verdicts", "",
             f"🔴 Red {v['breaking']} · 🟡 Yellow {v['notable']} · 🟢 Green {v['none']}", ""]
    if s["by_repo"]:
        lines += ["### By repository", "", "| repository | reviews | 🔴 | 🟡 | 🟢 | tokens in | tokens out | cost | posted | 👍 | 👎 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in s["by_repo"]:
            lines.append(f"| {r['repo']} | {r.get('reviews', 0)} | {r.get('breaking', 0)} | {r.get('notable', 0)} | "
                         f"{r.get('none', 0)} | {r.get('tokens_in', 0):,} | {r.get('tokens_out', 0):,} | "
                         f"{_money(r.get('cost_usd')) if 'cost_usd' in r else 'n/a'} | {r.get('posted', 0)} | "
                         f"{r.get('thumbs_up', 0)} | {r.get('thumbs_down', 0)} |")
        lines.append("")
    if s["by_version"]:
        lines += ["### By version", "", "| version | reviews | 🔴 | 🟡 | 🟢 | cost | median latency |", "|---|---|---|---|---|---|---|"]
        for r in s["by_version"]:
            lines.append(f"| {r['version']} | {r.get('reviews', 0)} | {r.get('breaking', 0)} | {r.get('notable', 0)} | "
                         f"{r.get('none', 0)} | {_money(r.get('cost_usd')) if 'cost_usd' in r else 'n/a'} | "
                         f"{_seconds(r.get('latency_median_s'))} |")
        lines.append("")
    lines += ["## Feedback", "",
              f"{f['posted']} reviews posted, {f['fetched']} with feedback fetched.", "",
              "| | |", "|---|---|",
              f"| 👍 | {f['thumbs_up']} |", f"| 👎 | {f['thumbs_down']} |",
              f"| reviews with a reaction | {f['with_reactions']} of {f['fetched']} |",
              f"| replies | {f['replies']} on {f['with_replies']} reviews |",
              f"| bot comment deleted | {f['comment_deleted']} |", f"| note removed from description | {f['note_removed']} |",
              f"| pull requests merged / closed / open | {f['pr_merged']} / {f['pr_closed']} / {f['pr_open']} |",
              f"| Red pull requests merged | {f['red_merged']} |", ""]
    if f["by_color"]:
        lines += ["| verdict | reviews | 👍 | 👎 |", "|---|---|---|---|"]
        for verdict in ("breaking", "notable", "none"):
            c = f["by_color"].get(verdict)
            if c:
                lines.append(f"| {verdict} | {c['reviews']} | {c['up']} | {c['down']} |")
        lines.append("")
    if s["needs_look"]:
        lines += ["### Needs a look (👎, a reply, or a deleted comment)", ""]
        for g in s["needs_look"]:
            why = []
            if g.get("thumbs_down"):
                why.append(f"👎 {g['reactors']}".strip())
            if g.get("comment_deleted"):
                why.append("comment deleted")
            if g.get("reply_count"):
                why.append(f"{g['reply_count']} repl{'y' if g['reply_count'] == 1 else 'ies'}: “{g['first_reply'][:160]}”")
            lines.append(f"- [{g['repo']}#{g['number']}]({g['url']}) {g.get('color')}: {'; '.join(why)}")
        lines.append("")
    return "\n".join(lines)


def render_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for g in grade_rows(rows):
        w.writerow(g)
    return buf.getvalue()


def render_json(rows):
    return json.dumps(grade_rows(rows), indent=1) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="compatibility-review-report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("COMPAT_REVIEW_DATA", "data"))
    ap.add_argument("--since", type=float, metavar="DAYS", help="only reviews from the last DAYS days")
    ap.add_argument("--repo", metavar="OWNER/REPO")
    ap.add_argument("--format", choices=["md", "csv", "json"], default="md")
    ap.add_argument("--out", help="write here instead of stdout")
    args = ap.parse_args(argv)
    db = DB(Path(args.data) / "compat-review.sqlite3")
    rows = db.rows(args.since, args.repo)
    if args.format == "csv":
        text = render_csv(rows)
    elif args.format == "json":
        text = render_json(rows)
    else:
        text = render_md(summarize(rows), window=f"last {args.since:g} days" if args.since else "all time", repo=args.repo)
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
