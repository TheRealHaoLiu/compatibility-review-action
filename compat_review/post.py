"""What the bot writes on a pull request: the note in the description, and the gradable comment.

Both carry a hidden record (head, base, diff hash, versions, verdict, usage). GitHub is the system of
record: with the database gone, the service reads these back and never reviews a diff twice.
"""

import json
import re

BEGIN = "<!-- compatibility-review:begin"  # the opening marker; the record and " -->" follow
END = "<!-- compatibility-review:end -->"
MARK = "<!-- compatibility-review "
BADGE = {"breaking": "🔴 Red", "notable": "🟡 Yellow", "none": "🟢 Green"}
ALERT = {"breaking": "CAUTION", "notable": "WARNING", "none": "TIP"}  # GitHub's colored callouts
MECHANISM = {
    "restricts_stored_at_runtime": "a new restriction applied at runtime to configuration already stored",
    "restricts_stored_on_resave": "a check that runs only when the stored value is saved again",
    "restricts_new_input_only": "a restriction on new input only",
    "permission": "an existing user, role, or token loses access",
    "removed": "a setting, field, endpoint, or option is removed or renamed",
    "default": "a default value changes",
    "output": "API output that clients may parse changes",
    "other": "other",
    "none": "none",
}


def record(result):
    """The machine-readable facts a grader, or a rebuilt database, needs later; kept small."""
    m = result.get("_meta", {})
    rec = {"version": m.get("prompt"), "rule": m.get("rule"), "model": m.get("model"),
           "head": m.get("head"), "base": m.get("base"), "diff_sha256": m.get("diff_sha256"),
           "verdict": result.get("verdict"), "mechanism": result.get("q_mechanism"),
           "prevalence": result.get("q_prevalence")}
    usage = m.get("usage") or {}
    if usage:
        rec["usage"] = {k: int(usage.get(k) or 0) for k in ("in", "out", "reasoning")}
    if m.get("latency_s") is not None:
        rec["latency_s"] = m["latency_s"]
    if m.get("posted_at"):
        rec["posted_at"] = m["posted_at"]
    return rec


def parse_note_record(body):
    """The hidden record out of the note in a PR description, or None."""
    m = re.search(re.escape(BEGIN) + r" (\{.*?\}) -->", body or "", re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def _lines(result):
    out = [f"**Upgrade compatibility: {BADGE[result['verdict']]}**", ""]
    if result.get("summary"):
        out += [result["summary"].strip(), ""]
    setup = (result.get("q_breaking_setup") or "none").strip()
    if not setup.lower().startswith("none"):
        out.append(f"- **Setup that stops working:** {setup}")
        out.append(f"- **How:** {MECHANISM.get(result.get('q_mechanism'), result.get('q_mechanism'))}; "
                   f"prevalence {result.get('q_prevalence')}; old behavior preserved by this change: "
                   f"{result.get('q_auto_preserved')}")
    if result.get("q_widens_access_or_changes_output") == "yes":
        out.append("- **Widens access or changes output that clients may parse.**")
    for f in result.get("findings") or []:
        who = f.get("who_breaks", "").strip().rstrip(".")
        out.append(f"- {who}: {f.get('how', '').strip()} ({f.get('evidence', '').strip()})")
    while out and out[-1] == "":
        out.pop()
    return out


def _callout(result, lines):
    """A GitHub alert block colored by verdict, holding `lines`."""
    return [f"> [!{ALERT[result['verdict']]}]", *(("> " + line) if line else ">" for line in lines)]


def note(result):
    """The section spliced into the PR description: a rule, then a colored callout."""
    m = result.get("_meta", {})
    head = (m.get("head") or "")[:7]
    lines = [f"{BEGIN} {json.dumps(record(result), separators=(',', ':'))} -->", "---",
             *_callout(result, [*_lines(result), "",
                                f"<sub>compatibility-review {m.get('prompt', '')} · reviewed {head} · advisory, does not block merging</sub>"]),
             END]
    return "\n".join(lines)


def splice(body, section):
    """Replace the bot's section in a PR description, or append it."""
    body = body or ""
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if pattern.search(body):
        return pattern.sub(lambda _: section, body)
    return body.rstrip() + ("\n\n" if body.strip() else "") + section + "\n"


def comment(result):
    """The gradable comment: the verdict again, what was reviewed, and the invitation."""
    m = result.get("_meta", {})
    rec = record(result)
    lines = [*_callout(result, _lines(result)), "",
             f"`version: prompt {m.get('prompt')} · rule {m.get('rule')} · model {m.get('model')}`  ",
             f"`reviewed: head {(m.get('head') or '')[:7]} against base {(m.get('base') or '')[:7]}`", "",
             "Is this verdict right? React 👍 if it is, 👎 if it is not, and reply to this comment to say why.",
             "", f"{MARK}{json.dumps(rec, separators=(',', ':'))} -->"]
    return "\n".join(lines)


def parse_record(comment_body):
    """The hidden record out of a bot comment, or None."""
    m = re.search(re.escape(MARK) + r"(\{.*?\}) -->", comment_body or "", re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None
