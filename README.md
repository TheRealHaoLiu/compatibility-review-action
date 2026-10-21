# compatibility-review-action

A CI bot that reads a pull request and appends an upgrade-compatibility note to it: **Red**, **Yellow**, or **Green**. The note is advisory and never blocks a merge. It answers one question for the human reviewing the PR: will something that works today in a customer installation stop working after this change ships?

**Summary of the method and the benchmark:** https://therealhaoliu.github.io/compatibility-review-action/

## How it decides, in short

- The model does not pick the verdict. It answers six factual questions about the change: which stored setup stops working, whether something existing changes, whether the old behavior is kept working, how common the setup is, whether access or output changes, and by what mechanism the break happens.
- A short fixed rule in code turns those answers into the verdict, so every Red traces to a named setup and a named reason, and the rule can be tuned without touching the prompt.
- A new deny-list, validation rule, or filter applied at runtime to configuration already stored in installations is Red regardless of how common the setup is. A check that fires only when the value is saved again is Yellow.
- Before the model reads the change, tooling adds the code around each changed spot and the places that call it, taken from the tree before the change. No model call is involved.
- One review per pull request in production. The benchmark ran three to measure consistency.

## Status

- `compat_review/` is the reviewer at version v9: the prompt, the output schema, the rule, the repository-facts builder, and a command that reviews one change in a git checkout.
- `docs/` is the published summary of v9.
- The GitHub Action that posts the note and the gradable comment is next; `.github/workflows/` is empty until then.
- The evaluation harness and its data live in a private repository, because the benchmark references changes that had not shipped when they were labeled.

## Run it on a change

Python 3.11 or newer; no dependencies for OpenAI-compatible endpoints, `pip install anthropic` for the Anthropic API.

```sh
pip install .
export OPENAI_BASE_URL=https://your-endpoint   # any OpenAI-compatible gateway
export OPENAI_API_KEY=...
compatibility-review --repo . --base origin/main --head HEAD \
    --title "Deny link-local addresses in the notification allow list" \
    --body-file pr-body.md --model your-model-name
```

The diff runs from the merge base of `--base` and `--head` to `--head`; the repository facts come from the tree at that merge base. Output is one JSON object: the model's findings and answers, the derived `verdict` (`breaking`, `notable`, `none`) and `color`, and a `_meta` block with the prompt and rule versions, the model, token usage, and the base and head commits reviewed.

Options: `--api responses|chat|anthropic` (default `responses`), `--effort low|medium|high` (default `medium`), `--no-enrich` to send the diff alone, `--dry-run` to print the assembled input without calling a model, `--out FILE` to save the JSON. `COMPAT_REVIEW_HEADERS` may hold a JSON object of extra request headers for a gateway.

## Repository settings

A `.compatibility-review.toml` at the repository root carries the two things only the repository owner knows:

```toml
# A paragraph about the product and who installs it, appended to the prompt.
context = "..."              # or: context_file = "docs/compatibility-context.md"

# A change touching only these paths is Green with no model call. Nothing is ignored unless
# the repository says so. `*` matches within a path segment, `**` across segments.
ignore = [".github/**", "**/tests/**", "docs/**", "*.md"]
```

Without the file, the generic product paragraph in `compat_review/context.md` is used and nothing is ignored.

## Layout

| Path | What it is |
| --- | --- |
| `compat_review/prompt.md` | The reviewer prompt: the question, what to look for, the six answers |
| `compat_review/schema.json` | The output schema the model is constrained to |
| `compat_review/derive.py` | The fixed rule from answers to verdict |
| `compat_review/enrich.py` | Repository facts: definitions around each change and one hop of callers (Python only for now) |
| `compat_review/scope.py` | Repository settings and path ignores |
| `compat_review/review.py` | The model call (Responses, Chat Completions, or Anthropic Messages) |
| `compat_review/cli.py` | `compatibility-review`: diff and facts from a git checkout, then the review |
| `tests/` | `python -m unittest discover -s tests` |
| `docs/` | The published summary |

## Planned action

- Runs on `pull_request` and appends the note to the PR description.
- Posts one comment per review carrying the bot version, the head and base SHA reviewed, and the verdict with its reason, and invites a thumbs up or thumbs down plus a reply saying why. Those reactions and replies are read back by a script to grade the bot per repository.
- Caches on the diff hash, so a re-push without changes costs nothing.

## License

Apache-2.0. See `LICENSE`.
