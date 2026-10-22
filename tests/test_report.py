import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from compat_review import report
from compat_review.db import DB
from test_db import result


def filled_db(tmp):
    db = DB(Path(tmp) / "compat-review.sqlite3")
    a = db.insert_review(result(1, "a", "d1", "breaking", tokens=(10000, 2000), comment_id=11), title="Deny link-local",
                         price_in=0.1, price_out=0.5)
    b = db.insert_review(result(2, "b", "d2", "notable", tokens=(5000, 1000), comment_id=12), price_in=0.1, price_out=0.5)
    db.insert_review(result(3, "c", "d3", "none", tokens=(0, 0), skipped=True))
    db.insert_review(result(4, "d", "d4", "none", tokens=(3000, 500), owner="p", repo="q"))
    db.upsert_feedback(a, {"thumbs_up": 0, "thumbs_down": 1, "reactors": [{"login": "bob", "content": "-1"}],
                           "replies": [{"login": "bob", "excerpt": "never stored", "mentions_review": True}], "reply_count": 1,
                           "pr_state": "closed", "merged": 1, "note_present": 1})
    db.upsert_feedback(b, {"thumbs_up": 2, "pr_state": "open", "merged": 0, "note_present": 0})
    return db


class ReportTest(unittest.TestCase):
    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = report.summarize(filled_db(tmp).rows())
            self.assertEqual((s["reviews"], s["modeled"], s["skipped"]), (4, 3, 1))
            self.assertEqual((s["tokens_in"], s["tokens_out"]), (18000, 3500))
            self.assertAlmostEqual(s["cost_usd"], (15000 * 0.1 + 3000 * 0.5) / 1e6)
            self.assertEqual(s["cost_known"], 2)
            self.assertEqual(s["verdicts"], {"breaking": 1, "notable": 1, "none": 2})
            self.assertEqual(s["repos"], ["o/r", "p/q"])
            self.assertEqual(s["by_repo"][0]["repo"], "o/r")
            self.assertEqual((s["by_repo"][0]["reviews"], s["by_repo"][0]["posted"], s["by_repo"][0]["thumbs_down"]), (3, 2, 1))
            self.assertEqual(s["by_version"][0]["reviews"], 3)
            self.assertEqual(s["latency_median_s"], 12.5)
            f = s["feedback"]
            self.assertEqual((f["posted"], f["fetched"], f["thumbs_up"], f["thumbs_down"], f["replies"]), (2, 2, 2, 1, 1))
            self.assertEqual((f["pr_merged"], f["pr_open"], f["red_merged"], f["note_removed"], f["comment_deleted"]), (1, 1, 1, 1, 0))
            self.assertEqual(f["by_color"]["breaking"], {"up": 0, "down": 1, "reviews": 1})
            self.assertEqual([g["number"] for g in s["needs_look"]], [1])
            self.assertEqual(s["needs_look"][0]["reactors"], "bob:-1")
            self.assertEqual(s["needs_look"][0]["first_reply"], "never stored")
            json.dumps(s)  # serializable for the dashboard

    def test_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = filled_db(tmp).rows()
            md = report.render_md(report.summarize(rows), window="last 7 days", repo=None)
            self.assertIn("| reviews | 4 (3 by the model, 1 skipped by ignore rules) |", md)
            self.assertIn("🔴 Red 1 · 🟡 Yellow 1 · 🟢 Green 2", md)
            self.assertIn("[o/r#1](https://github.com/o/r/pull/1) Red: 👎 bob:-1; 1 reply: “never stored”", md)
            text = report.render_csv(rows)
            parsed = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(len(parsed), 4)
            self.assertEqual(parsed[0]["repo"], "p/q")  # newest first
            one = next(r for r in parsed if r["number"] == "1")
            self.assertEqual((one["url"], one["title"], one["thumbs_down"], one["first_reply"], one["merged"]),
                             ("https://github.com/o/r/pull/1", "Deny link-local", "1", "never stored", "1"))
            self.assertEqual(len(json.loads(report.render_json(rows))), 4)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            filled_db(tmp)
            out = Path(tmp) / "r.csv"
            self.assertEqual(report.main(["--data", tmp, "--format", "csv", "--repo", "o/r", "--out", str(out)]), 0)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(out.read_text())))), 3)

    def test_empty(self):
        s = report.summarize([])
        self.assertEqual(s["reviews"], 0)
        self.assertIsNone(s["cost_usd"])
        self.assertIn("no prices configured", report.render_md(s))


if __name__ == "__main__":
    unittest.main()
