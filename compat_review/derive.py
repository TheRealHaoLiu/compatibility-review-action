"""Verdicts derived by a fixed rule from the reviewer's factual answers (schema.json).

The model never picks the verdict. It names the setup that stops working (q_breaking_setup),
says whether an existing path changes (q_changes_existing_path), whether the change itself keeps
the old behavior working (q_auto_preserved), how common the setup is (q_prevalence), whether
access widens or output changes (q_widens_access_or_changes_output), and by what mechanism the
break happens (q_mechanism). This module turns those answers into:

  breaking  (Red)    a setup is named, an existing path changes, nothing preserves the old
                     behavior, and either the setup is common or the mechanism is a new or
                     extended deny-list, validation rule, or runtime filter applied at runtime
                     to configuration already stored in installations. That last class is Red
                     whatever its prevalence: the stored value stays in place and silently
                     stops working at its next use.
  notable   (Yellow) a setup is named, or access widens, or output changes, but the Red
                     conditions do not all hold. A check that fires only when a stored value
                     is saved or re-applied again lands here.
  none      (Green)  otherwise.

Deliberately absent from the rule: whether the old behavior ever shipped, whether it was a
bug, and whether the blocked value is a plausible setup. Those are the reviewer's questions
after the signal, not the bot's reasons to stay quiet.

The relaxed rules exist for experiments on how much the prevalence answer should count:
  no_rare  alarm unless the setup is rare
  no_prev  alarm whenever a setup breaks and the change does not preserve the old behavior
"""

ALARM_PREVALENCE = {
    "strict": ("common",),
    "no_rare": ("common", "uncommon"),
    "no_prev": ("common", "uncommon", "rare", "n/a"),
}


def _broke(q):
    return not q["q_breaking_setup"].strip().lower().startswith("none")


def derive(q, rule="strict"):
    broke = _broke(q)
    restricts_stored = q.get("q_mechanism") == "restricts_stored_at_runtime"
    if (q["q_changes_existing_path"] != "no" and broke and q["q_auto_preserved"] != "yes"
            and (q["q_prevalence"] in ALARM_PREVALENCE[rule] or restricts_stored)):
        return "breaking"
    if broke or q["q_widens_access_or_changes_output"] == "yes":
        return "notable"
    return "none"


COLOR = {"breaking": "Red", "notable": "Yellow", "none": "Green"}


def add_verdicts(result):
    """Set result['verdict'] and result['color'] from the answers in place, and return it."""
    if "q_breaking_setup" not in result:
        return result
    result["verdict"] = derive(result)
    result["color"] = COLOR[result["verdict"]]
    return result
