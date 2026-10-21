import unittest

from compat_review.derive import derive


def answers(**over):
    q = {"q_changes_existing_path": "yes", "q_breaking_setup": "A stored setting X now fails at use",
         "q_mechanism": "other", "q_prevalence": "uncommon", "q_auto_preserved": "no",
         "q_widens_access_or_changes_output": "no"}
    q.update(over)
    return q


class RuleTest(unittest.TestCase):
    def test_common_setup_is_breaking(self):
        self.assertEqual(derive(answers(q_prevalence="common")), "breaking")

    def test_uncommon_setup_is_notable(self):
        self.assertEqual(derive(answers(q_prevalence="uncommon")), "notable")

    def test_runtime_restriction_on_stored_config_is_breaking_whatever_the_prevalence(self):
        for prev in ("common", "uncommon", "rare", "n/a"):
            self.assertEqual(derive(answers(q_mechanism="restricts_stored_at_runtime", q_prevalence=prev)), "breaking")

    def test_resave_only_is_notable(self):
        self.assertEqual(derive(answers(q_mechanism="restricts_stored_on_resave", q_prevalence="rare")), "notable")

    def test_preserved_old_behavior_is_notable(self):
        self.assertEqual(derive(answers(q_prevalence="common", q_auto_preserved="yes")), "notable")

    def test_new_path_only_is_notable(self):
        self.assertEqual(derive(answers(q_prevalence="common", q_changes_existing_path="no")), "notable")

    def test_nothing_breaks_is_none(self):
        self.assertEqual(derive(answers(q_breaking_setup="none", q_mechanism="none", q_prevalence="n/a")), "none")

    def test_widened_access_is_notable(self):
        self.assertEqual(derive(answers(q_breaking_setup="none", q_mechanism="none", q_prevalence="n/a",
                                        q_widens_access_or_changes_output="yes")), "notable")


if __name__ == "__main__":
    unittest.main()
