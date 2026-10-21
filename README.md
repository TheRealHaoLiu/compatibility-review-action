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

- `compat_review/` is the reviewer at version v9: the prompt, the output schema, the rule, the repository-facts builder, a command that reviews one change in a checkout, and a webhook service that runs as a GitHub App.
- `docs/` is the published summary of v9.
- The evaluation harness and its data live in a private repository, because the benchmark references changes that had not shipped when they were labeled.

## Run the webhook service as a GitHub App

The service receives `pull_request` events, reviews each new diff once, writes the note into the PR description, and posts one gradable comment per review. One process, standard library plus PyJWT, with bare mirrors of the repositories it reviews under its data directory.

1. **Create a GitHub App** (Settings, Developer settings, GitHub Apps, New GitHub App). Webhook URL: your service's `/webhook` (for a trial, a smee.io channel). Set a webhook secret. Permissions: Pull requests read and write, Contents read, Metadata read. Subscribe to the Pull request event. Generate a private key and note the App ID. Install the App on the repositories to review.
2. **Run it.**

   ```sh
   pip install ".[service]"
   export GITHUB_APP_ID=12345
   export GITHUB_APP_PRIVATE_KEY_FILE=./app.pem
   export GITHUB_WEBHOOK_SECRET=...
   export OPENAI_BASE_URL=https://your-endpoint   # any OpenAI-compatible gateway
   export OPENAI_API_KEY=...
   export COMPAT_REVIEW_MODEL=your-model-name
   compatibility-review-service --port 8000          # add --no-post to review without writing to GitHub
   ```

   For a trial on a laptop, forward the App's webhooks to it: `npx smee-client -u https://smee.io/<channel> -t http://localhost:8000/webhook`.
3. **Review one pull request by hand** with `compatibility-review-service --once owner/repo#123`. With `GITHUB_TOKEN` set to a personal token, this works before any App exists.

What the service does per event: verify the signature, queue the PR, fetch the mirror and the PR head, diff from the merge base, repository facts from the tree at the merge base, read the repository's `.compatibility-review.toml` at that commit, one model call, then the note and the comment. The result JSON lands in `data/reviews/<owner>-<repo>/<number>-<diff sha>.json`; a diff with the same sha256 is not reviewed again, so a re-push without changes costs nothing. Draft PRs are skipped unless `COMPAT_REVIEW_SKIP_DRAFTS=0`. Other settings: `COMPAT_REVIEW_API` (`responses`, `chat`, `anthropic`), `COMPAT_REVIEW_EFFORT`, `COMPAT_REVIEW_BASE_URL`, `COMPAT_REVIEW_DATA`, `COMPAT_REVIEW_HEADERS` (a JSON object of extra request headers for a gateway).

A `Dockerfile` builds the service image; mount `/data` and pass the environment above.

### What it posts

In the PR description, between `<!-- compatibility-review:begin -->` and `<!-- compatibility-review:end -->`: the verdict, the summary, the setup that stops working and how, and the version and commit reviewed. The section is replaced on each review, so the description holds one note.

As a comment: the same verdict and reason, the prompt, rule and model versions, the head and base SHAs reviewed, and the invitation to react 👍 or 👎 and reply with the reason. The comment ends with a hidden `<!-- compatibility-review {...} -->` record, so grading the bot later needs nothing but the GitHub API: list the bot's comments, read the reactions and replies, join on the record.

## Run it on a change by hand

```sh
pip install .
export OPENAI_BASE_URL=https://your-endpoint
export OPENAI_API_KEY=...
compatibility-review --repo . --base origin/main --head HEAD \
    --title "Deny link-local addresses in the notification allow list" \
    --body-file pr-body.md --model your-model-name
```

The diff runs from the merge base of `--base` and `--head` to `--head`; the repository facts come from the tree at that merge base. Output is one JSON object: the model's findings and answers, the derived `verdict` (`breaking`, `notable`, `none`) and `color`, and a `_meta` block with the prompt and rule versions, the model, token usage, and the commits reviewed. `--dry-run` prints the assembled input without calling a model; `--no-enrich` sends the diff alone; `--api`, `--effort`, `--out` as for the service.

## Repository settings

A `.compatibility-review.toml` at the repository root carries the two things only the repository owner knows:

```toml
# A paragraph about the product and who installs it, appended to the prompt.
context = "..."              # or: context_file = "docs/compatibility-context.md"

# A change touching only these paths is Green with no model call. Nothing is ignored unless
# the repository says so. `*` matches within a path segment, `**` across segments.
ignore = [".github/**", "**/tests/**", "docs/**", "*.md"]
```

Without the file, the generic product paragraph in `compat_review/context.md` is used and nothing is ignored. The service reads the file at the merge base of the change, so a PR cannot change its own review settings.

## Layout

| Path | What it is |
| --- | --- |
| `compat_review/prompt.md` | The reviewer prompt: the question, what to look for, the six answers |
| `compat_review/schema.json` | The output schema the model is constrained to |
| `compat_review/derive.py` | The fixed rule from answers to verdict |
| `compat_review/enrich.py` | Repository facts: definitions around each change and one hop of callers (Python only for now) |
| `compat_review/scope.py` | Repository settings and path ignores |
| `compat_review/change.py` | Diff from the merge base, tree extraction, the assembled input |
| `compat_review/review.py` | The model call (Responses, Chat Completions, or Anthropic Messages) |
| `compat_review/cli.py` | `compatibility-review`: one change in a checkout |
| `compat_review/service.py` | `compatibility-review-service`: the webhook server and worker |
| `compat_review/github.py` | GitHub App tokens and the REST calls used |
| `compat_review/post.py` | The note and the gradable comment |
| `compat_review/mirror.py` | Bare mirrors of reviewed repositories |
| `tests/` | `python -m unittest discover -s tests` |
| `docs/` | The published summary |

## License

Apache-2.0. See `LICENSE`.
