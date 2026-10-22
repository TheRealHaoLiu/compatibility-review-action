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

- `compat_review/` is the reviewer at version v9: the prompt, the output schema, the rule, the repository-facts builder, a command that reviews one change in a checkout, and a service that reviews pull requests by polling or as a webhook GitHub App.
- `docs/` is the published summary of v9.
- The evaluation harness and its data live in a private repository, because the benchmark references changes that had not shipped when they were labeled.

## Configuration

Configuration goes through [Dynaconf](https://www.dynaconf.com/). Copy `settings.example.toml` to `settings.toml` in the directory you run from, or any parent of it, and put tokens in `.secrets.toml` in the same format, which git ignores. Every key is also an environment variable with the prefix `COMPAT_REVIEW_`, which overrides the file: `COMPAT_REVIEW_MODEL`, `COMPAT_REVIEW_POLL_REPOS`, and so on. The conventional `GITHUB_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_PRIVATE_KEY_FILE`, `GITHUB_WEBHOOK_SECRET`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `PORT` are honored when the prefixed form is absent. Command-line flags override everything, and `--config FILE` names a settings file explicitly. The example file documents every key; a value that fails validation stops the program at startup with a message naming it.

## Run the service

The service reviews each new diff of a pull request once, writes the note into the PR description, and posts one gradable comment per review. One process, with Dynaconf and PyJWT as its dependencies, and bare mirrors of the repositories it reviews under its data directory. It learns about pull requests in one of two ways, or both at once:

- **Poll mode** lists the open pull requests of named repositories every interval and reviews those whose head commit it has not seen. It needs a token and nothing else: no App, no public endpoint. Start here.
- **Webhook mode** receives `pull_request` events as a GitHub App, so a review starts seconds after a push. It needs an App registration and a reachable `/webhook` URL.

Common to both, as environment variables here; the same keys go in `settings.toml`:

```sh
pip install ".[service]"
export OPENAI_BASE_URL=https://your-endpoint   # any OpenAI-compatible gateway
export OPENAI_API_KEY=...
export COMPAT_REVIEW_MODEL=your-model-name
```

### Poll mode

```sh
export GITHUB_TOKEN=$(gh auth token)           # or a fine-grained token: pull requests read/write, contents read
compatibility-review-service --poll owner/repo --no-webhook --no-post
```

Watch the log, or open the dashboard at `http://localhost:8000/`: the first scan queues every open, non-draft pull request updated in the last 7 days, the worker reviews them one at a time, and each result lands in the database under `data/`. Older open pull requests are treated as abandoned and left alone until a push moves their head; `--max-age DAYS` (`COMPAT_REVIEW_POLL_MAX_AGE_DAYS`) changes the cutoff, and 0 reviews everything. Drop `--no-post` once a review looks right; results stored while posting was off are posted on the next scan without a new model call. `--poll` repeats or takes a comma-separated list (`COMPAT_REVIEW_POLL_REPOS` in the environment does the same, for a container), `--interval` sets the seconds between scans (`COMPAT_REVIEW_POLL_INTERVAL`, default 60). Polling costs one API request per hundred open pull requests per scan. A pull request that fails to review is not retried until its head moves or the service restarts.

**One pull request by hand:** `compatibility-review-service --once owner/repo#123` reviews it now and prints the result.

### Webhook mode

1. **Create a GitHub App** (Settings, Developer settings, GitHub Apps, New GitHub App). Webhook URL: your service's `/webhook` (for a trial, a smee.io channel). Set a webhook secret. Permissions: Pull requests read and write, Contents read, Metadata read. Subscribe to the Pull request event. Generate a private key and note the App ID. Install the App on the repositories to review.
2. **Run it.**

   ```sh
   export GITHUB_APP_ID=12345
   export GITHUB_APP_PRIVATE_KEY_FILE=./app.pem
   export GITHUB_WEBHOOK_SECRET=...
   compatibility-review-service --port 8000          # add --no-post to review without writing to GitHub
   ```

   For a trial on a laptop, forward the App's webhooks to it: `npx smee-client -u https://smee.io/<channel> -t http://localhost:8000/webhook`.

App credentials work in poll mode and with `--once` too: the service looks up the App's installation on each repository. Adding `--poll owner/repo` to a webhook service makes it also sweep for pull requests whose events were missed, for example while it was down.

### What happens per pull request

Fetch the mirror and the PR head, diff from the merge base, repository facts from the tree at the merge base, read the repository's `.compatibility-review.toml` at that commit, one model call, store the result, then the note and the comment. The result JSON lands in `data/reviews/<owner>-<repo>/<number>-<diff sha>.json`; a diff with the same sha256 is not reviewed again, so a re-push without changes costs nothing. The result is stored before anything is posted, so a failed post never costs a second model call. Draft PRs are skipped unless `COMPAT_REVIEW_SKIP_DRAFTS=0`. Other settings: `COMPAT_REVIEW_API` (`responses`, `chat`, `anthropic`), `COMPAT_REVIEW_EFFORT`, `COMPAT_REVIEW_BASE_URL`, `COMPAT_REVIEW_DATA`, `COMPAT_REVIEW_HEADERS` (a JSON object of extra request headers for a gateway).

A `Dockerfile` builds the service image; mount `/data` and pass the environment above. With `COMPAT_REVIEW_POLL_REPOS` set, the container polls; with the App variables set, it serves webhooks on port 8000; with both, it does both.

### Dashboard, database and exports

Every review goes into `compat-review.sqlite3` under the data directory: the verdict and the answers behind it, the prompt, rule and model versions, tokens, cost, latency, the commits reviewed, when it was posted and as which comment, and the full result. A background thread then keeps the feedback on each posted review current: 👍 and 👎 on the bot's comment and who gave them, human replies after it with an excerpt, whether the comment or the note in the description was deleted, and whether the pull request merged or closed. It refreshes every 30 minutes for a week after posting, every six hours for two months, then weekly, with conditional requests that GitHub does not count against the rate limit when nothing changed. `--no-feedback` turns it off; `COMPAT_REVIEW_FEEDBACK_INTERVAL` sets the first cadence.

The service serves a live dashboard at `/`: a time window and a repository filter, spend (reviews, tokens, cost, latency), the verdict mix, per-repository and per-version tables, the feedback tallies by verdict, a "needs a look" list of reviews with a 👎, a reply, or a deleted comment, and the recent reviews with links to their pull requests. It refreshes itself every 30 seconds. The same data exports with the dashboard's current filters from `/export.csv` (one row per review, the grade sheet), `/export.json`, and `/report.md` (the summary as Markdown), or from the command line:

```sh
compatibility-review-report --data ./data --since 30 --repo owner/repo --format csv --out reviews.csv
```

Costs need the model's prices in dollars per million tokens, `COMPAT_REVIEW_PRICE_IN_PER_M` and `COMPAT_REVIEW_PRICE_OUT_PER_M`; without them the dashboard shows n/a. Set `COMPAT_REVIEW_DASHBOARD_TOKEN` and the dashboard and exports require `?token=...` or an `Authorization: Bearer` header; `/healthz` never does. If the service's port is reachable from the internet for webhooks, set the token or keep the dashboard behind your proxy, because it names the pull requests it reviewed.

### Storage

The data directory holds three things, and none of them needs watching:

- `mirrors/`: one bare clone per repository, branches and tags only, so the cost is the repository's history once, whatever the number of branches reviewed against. Branches deleted upstream are pruned on every fetch. A pull request's ref is dropped as soon as its review is stored, so the objects it alone kept alive are reclaimed by git's own garbage collection. All mirrors together are kept under a budget, `COMPAT_REVIEW_MAX_MIRRORS_GB` or `--max-mirrors-gb` (default 50): past it, the least recently used mirrors are deleted and cloned again on their next review. The service logs the usage against the budget at startup and whenever it evicts.
- `compat-review.sqlite3`: every review and the feedback on it, a few kilobytes per review. This is the record the feedback is graded against and is kept. Result files under `reviews/` from earlier versions are imported into it at startup.
- `tmp/`: the tree a review extracts for the repository facts, removed when the review ends. Anything a hard kill leaves here is removed at the next start.

One review at a time per data directory, across processes: a lock file in it is held for each review and for the startup cleanup. A `--once` run alongside the running service waits for the current review and then goes, and two services pointed at one data directory take turns rather than racing each other's trees and mirrors.

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
| `compat_review/config.py` | The configuration: Dynaconf, every key with its default and validation |
| `settings.example.toml` | Every setting, documented |
| `compat_review/service.py` | `compatibility-review-service`: the poller, the webhook server, the worker, the feedback thread, the dashboard routes |
| `compat_review/db.py` | The SQLite record of reviews and feedback |
| `compat_review/feedback.py` | Reactions, replies, and the pull request's fate, fetched with conditional requests |
| `compat_review/report.py` | `compatibility-review-report`: the summary and the grade sheet, also behind the dashboard's exports |
| `compat_review/dashboard.html` | The dashboard page, no external assets |
| `compat_review/github.py` | GitHub App tokens and the REST calls used |
| `compat_review/post.py` | The note and the gradable comment |
| `compat_review/mirror.py` | Bare mirrors of reviewed repositories |
| `tests/` | `python -m unittest discover -s tests` |
| `docs/` | The published summary |

## License

Apache-2.0. See `LICENSE`.
