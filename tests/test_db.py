import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from compat_review.db import DB, cost_usd, review_fields


def result(number, head, diff_sha, verdict="notable", tokens=(1000, 200), comment_id=None, posted_at=None,
           skipped=False, owner="o", repo="r"):
    meta = {"owner": owner, "repo": repo, "number": number, "head": head, "base": "b" * 40, "diff_sha256": diff_sha,
            "prompt": "v9", "rule": "v9", "model": "m", "api": "responses", "effort": "medium", "enriched": True,
            "usage": {"in": tokens[0], "out": tokens[1], "reasoning": 50}, "latency_s": 12.5}
    if comment_id:
        meta.update({"comment_id": comment_id, "posted_at": posted_at or "2026-10-22T10:00:00+00:00"})
    if skipped:
        meta.update({"skipped": "out of scope", "paths": ["docs/a.md"], "usage": {}})
    return {"verdict": verdict, "color": {"breaking": "Red", "notable": "Yellow", "none": "Green"}[verdict],
            "summary": f"summary {number}", "q_mechanism": "output", "q_prevalence": "rare",
            "q_auto_preserved": "no", "q_widens_access_or_changes_output": "yes", "q_breaking_setup": "x",
            "findings": [], "_meta": meta}


class DBTest(unittest.TestCase):
    def test_insert_find_and_posting(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "x.sqlite3")
            rid = db.insert_review(result(1, "aaa", "d1"), title="T", base_ref="main", price_in=0.1, price_out=0.5)
            row = db.find_review("o", "r", 1, "d1")
            self.assertEqual((row["id"], row["title"], row["base_ref"], row["verdict"], row["tokens_in"]), (rid, "T", "main", "notable", 1000))
            self.assertAlmostEqual(row["cost_usd"], (1000 * 0.1 + 200 * 0.5) / 1e6)
            self.assertIsNone(row["comment_id"])
            db.mark_posted(rid, 77, "2026-10-22T11:00:00+00:00")
            row = db.find_review("o", "r", 1, "d1")
            self.assertEqual((row["comment_id"], row["posted_at"]), (77, "2026-10-22T11:00:00+00:00"))
            # storing the same diff again keeps the posting state and returns the same id
            self.assertEqual(db.insert_review(result(1, "aaa", "d1")), rid)
            self.assertEqual(db.find_review("o", "r", 1, "d1")["comment_id"], 77)
            self.assertIsNone(db.find_review("o", "r", 1, "other"))
            self.assertEqual(db.result(rid)["summary"], "summary 1")

    def test_reviewed_heads_and_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "x.sqlite3")
            db.insert_review(result(1, "aaa", "d1", comment_id=5), reviewed_at="2026-10-01T00:00:00+00:00")
            db.insert_review(result(1, "aa2", "d2", comment_id=6), reviewed_at="2026-10-02T00:00:00+00:00")
            db.insert_review(result(2, "bbb", "d3"))  # not posted
            db.insert_review(result(9, "zzz", "d9", owner="p", repo="q"))
            self.assertEqual(db.reviewed_heads("o", "r"), {1: "aa2", 2: "bbb"})
            self.assertEqual(db.reviewed_heads("o", "r", posted_only=True), {1: "aa2"})
            self.assertEqual(db.repos(), ["o/r", "p/q"])

    def test_rows_window_and_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "x.sqlite3")
            old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
            db.insert_review(result(1, "aaa", "d1"), reviewed_at=old)
            rid = db.insert_review(result(2, "bbb", "d2", comment_id=3))
            db.insert_review(result(3, "ccc", "d3", owner="p", repo="q"))
            db.upsert_feedback(rid, {"thumbs_up": 2, "replies": [{"login": "x", "excerpt": "hi"}], "reply_count": 1,
                                     "pr_state": "closed", "merged": 1})
            self.assertEqual([r["number"] for r in db.rows()], [3, 2, 1])
            self.assertEqual([r["number"] for r in db.rows(since_days=30)], [3, 2])
            self.assertEqual([r["number"] for r in db.rows(repo="p/q")], [3])
            self.assertEqual([r["number"] for r in db.rows(limit=1)], [3])
            two = next(r for r in db.rows() if r["number"] == 2)
            self.assertEqual((two["thumbs_up"], two["reply_count"], two["merged"]), (2, 1, 1))
            self.assertEqual(json.loads(two["replies"])[0]["login"], "x")
            self.assertIsNotNone(two["feedback_fetched_at"])
            self.assertNotIn("result_json", two)
            self.assertIsNone(next(r for r in db.rows() if r["number"] == 1)["thumbs_up"])

    def test_due_for_feedback_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "x.sqlite3")
            now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
            ts = lambda **kw: (now - timedelta(**kw)).isoformat(timespec="seconds")  # noqa: E731
            fresh = db.insert_review(result(1, "a", "d1", comment_id=1, posted_at=ts(days=1)))
            week_old = db.insert_review(result(2, "b", "d2", comment_id=2, posted_at=ts(days=20)))
            ancient = db.insert_review(result(3, "c", "d3", comment_id=3, posted_at=ts(days=200)))
            db.insert_review(result(4, "d", "d4"))  # never posted: never due
            self.assertEqual(sorted(r["id"] for r in db.due_for_feedback(1800, now)), [fresh, week_old, ancient])
            db.upsert_feedback(fresh, {"fetched_at": ts(minutes=10)})
            db.upsert_feedback(week_old, {"fetched_at": ts(hours=5)})
            db.upsert_feedback(ancient, {"fetched_at": ts(days=6)})
            self.assertEqual(db.due_for_feedback(1800, now), [])
            db.upsert_feedback(fresh, {"fetched_at": ts(minutes=31)})
            db.upsert_feedback(week_old, {"fetched_at": ts(hours=7)})
            db.upsert_feedback(ancient, {"fetched_at": ts(days=8)})
            self.assertEqual(sorted(r["id"] for r in db.due_for_feedback(1800, now)), [fresh, week_old, ancient])

    def test_import_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "reviews" / "o-r"
            root.mkdir(parents=True)
            (root / "1-d1.json").write_text(json.dumps(result(1, "aaa", "d1")))
            (root / "2-d2.json").write_text(json.dumps(result(2, "bbb", "d2", comment_id=4)))
            (root / "bad.json").write_text("{not json")
            (root / "nometa.json").write_text(json.dumps({"verdict": "none", "_meta": {}}))
            db = DB(Path(tmp) / "x.sqlite3")
            self.assertEqual(db.import_json(Path(tmp) / "reviews"), 2)
            self.assertEqual(db.import_json(Path(tmp) / "reviews"), 0)
            self.assertEqual(db.find_review("o", "r", 2, "d2")["comment_id"], 4)
            self.assertEqual(db.import_json(Path(tmp) / "nowhere"), 0)

    def test_fields(self):
        self.assertIsNone(cost_usd(10, 10, None, 0.5))
        f = review_fields(result(1, "a", "d", skipped=True))
        self.assertEqual((f["skipped"], f["tokens_in"], f["cost_usd"]), (1, 0, None))


if __name__ == "__main__":
    unittest.main()
