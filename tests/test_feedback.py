import unittest

from compat_review import feedback
from compat_review.github import GitHubError
from compat_review.post import BEGIN


class FakeGitHub:
    """Answers get_if_changed from a table of path -> (data, etag); 304 when the etag matches; 404 when absent."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def get_if_changed(self, path, etag=None):
        self.calls.append((path, etag))
        key = path.split("?")[0]
        if key not in self.table:
            raise GitHubError(f"GET {path}: 404", status=404)
        data, current = self.table[key]
        if etag and etag == current:
            return None, etag
        return data, current


def row(**over):
    base = {"id": 1, "owner": "o", "repo": "r", "number": 5, "comment_id": 900, "posted_at": "2026-10-22T10:00:00+00:00",
            "verdict": "breaking", "thumbs_up": None, "thumbs_down": None, "other_reactions": None, "reactors": None,
            "replies": None, "reply_count": None, "comment_deleted": None, "note_present": None, "pr_state": None,
            "merged": None, "merged_at": None, "closed_at": None, "etag_reactions": None, "etag_comments": None,
            "etag_pull": None}
    base.update(over)
    return base


COMMENTS = [
    {"id": 900, "user": {"login": "bot", "type": "Bot"}, "created_at": "2026-10-22T10:00:00Z", "body": "the review"},
    {"id": 901, "user": {"login": "alice", "type": "User"}, "created_at": "2026-10-22T11:00:00Z",
     "html_url": "https://github.com/o/r/pull/5#issuecomment-901", "body": "The verdict is wrong, this setting was never stored."},
    {"id": 902, "user": {"login": "ci", "type": "Bot"}, "created_at": "2026-10-22T11:30:00Z", "body": "build passed"},
    {"id": 899, "user": {"login": "bob", "type": "User"}, "created_at": "2026-10-22T09:59:30Z", "body": "before the bot"},
]


class FeedbackTest(unittest.TestCase):
    def test_first_refresh(self):
        gh = FakeGitHub({
            "/repos/o/r/issues/comments/900/reactions": ([{"content": "+1", "user": {"login": "alice"}},
                                                          {"content": "-1", "user": {"login": "bob"}},
                                                          {"content": "heart", "user": {"login": "eve"}}], "r1"),
            "/repos/o/r/issues/5/comments": (COMMENTS, "c1"),
            "/repos/o/r/pulls/5": ({"state": "closed", "merged": True, "merged_at": "2026-10-23T00:00:00Z",
                                    "closed_at": "2026-10-23T00:00:00Z", "body": "desc\n" + BEGIN + " note"}, "p1"),
        })
        out = feedback.refresh(gh, row())
        self.assertEqual((out["thumbs_up"], out["thumbs_down"], out["other_reactions"]), (1, 1, 1))
        self.assertEqual(out["reactors"][1], {"login": "bob", "content": "-1"})
        self.assertEqual(out["reply_count"], 1)
        self.assertEqual(out["replies"][0]["login"], "alice")
        self.assertTrue(out["replies"][0]["mentions_review"])
        self.assertEqual(out["replies"][0]["url"], "https://github.com/o/r/pull/5#issuecomment-901")
        self.assertEqual(out["comment_deleted"], None)
        self.assertEqual((out["pr_state"], out["merged"], out["note_present"]), ("closed", 1, 1))
        self.assertEqual((out["etag_reactions"], out["etag_comments"], out["etag_pull"]), ("r1", "c1", "p1"))
        self.assertIn("since=2026-10-22T09:59:00Z", gh.calls[1][0])

    def test_unchanged_keeps_stored_values(self):
        gh = FakeGitHub({
            "/repos/o/r/issues/comments/900/reactions": ([], "r1"),
            "/repos/o/r/issues/5/comments": ([], "c1"),
            "/repos/o/r/pulls/5": ({}, "p1"),
        })
        stored = row(thumbs_up=3, reply_count=2, replies='[{"login": "x"}]', pr_state="open", merged=0, note_present=1,
                     etag_reactions="r1", etag_comments="c1", etag_pull="p1")
        out = feedback.refresh(gh, stored)
        self.assertEqual((out["thumbs_up"], out["reply_count"], out["pr_state"], out["note_present"]), (3, 2, "open", 1))
        self.assertEqual([c[1] for c in gh.calls], ["r1", "c1", "p1"])

    def test_deleted_comment(self):
        gh = FakeGitHub({
            "/repos/o/r/issues/5/comments": ([c for c in COMMENTS if c["id"] != 900], "c2"),
            "/repos/o/r/pulls/5": ({"state": "open", "merged": False, "body": "desc without the note"}, "p1"),
        })
        out = feedback.refresh(gh, row(thumbs_up=4))
        self.assertEqual((out["comment_deleted"], out["thumbs_up"], out["thumbs_down"]), (1, 0, 0))
        self.assertEqual(out["note_present"], 0)
        self.assertEqual(out["reply_count"], 1)  # alice's comment still counts, after the posting time

    def test_resolves_the_comment_for_a_review_recovered_from_a_note(self):
        mine = {"id": 950, "user": {"login": "bot", "type": "Bot"}, "created_at": "2026-10-22T10:00:00Z",
                "body": "the review\n<!-- compatibility-review {\"diff_sha256\": \"dd\", \"verdict\": \"breaking\"} -->"}
        gh = FakeGitHub({
            "/repos/o/r/issues/5/comments": ([mine, COMMENTS[1]], "c1"),
            "/repos/o/r/issues/comments/950/reactions": ([{"content": "+1", "user": {"login": "a"}}], "r1"),
            "/repos/o/r/pulls/5": ({"state": "open", "merged": False, "body": "x"}, "p1"),
        })
        out = feedback.refresh(gh, row(comment_id=None, diff_sha256="dd"))
        self.assertEqual((out["resolved_comment_id"], out["resolved_posted_at"], out["thumbs_up"], out["reply_count"]),
                         (950, "2026-10-22T10:00:00Z", 1, 1))
        # no such comment any more: marked deleted, the pull request's state still fetched
        gh2 = FakeGitHub({
            "/repos/o/r/issues/5/comments": ([COMMENTS[1]], "c1"),
            "/repos/o/r/pulls/5": ({"state": "closed", "merged": True, "body": "x"}, "p1"),
        })
        out = feedback.refresh(gh2, row(comment_id=None, diff_sha256="dd"))
        self.assertEqual((out["comment_deleted"], out["pr_state"], out["merged"], out.get("resolved_comment_id")), (1, "closed", 1, None))

    def test_other_errors_propagate(self):
        class Broken:
            def get_if_changed(self, path, etag=None):
                raise GitHubError("GET: 500", status=500)

        with self.assertRaises(GitHubError):
            feedback.refresh(Broken(), row())


if __name__ == "__main__":
    unittest.main()
