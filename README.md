# compatibility-review-action

A CI bot that reads a pull request and appends an upgrade-compatibility note to it: **Red**, **Yellow**, or **Green**. The note is advisory and never blocks a merge. It answers one question for the human reviewing the PR: will something that works today in a customer installation stop working after this change ships?

**Summary of the method and the benchmark:** https://therealhaoliu.github.io/compatibility-review-action/

## Status

The GitHub Action is being built in this repository. What exists today:

- `docs/` holds the published summary of the current version (v9): what the bot does, how it decides, the benchmark, results, known limits, the feedback loop, and next steps.
- The evaluation harness and its data live in a private repository, because the benchmark references changes that had not shipped when they were labeled.

## How it decides, in short

- The model does not pick the verdict. It answers six factual questions about the change: which stored setup stops working, whether something existing changes, whether the old behavior is kept working, how common the setup is, whether access or output changes, and by what mechanism the break happens.
- A short fixed rule in code turns those answers into the verdict, so every Red traces to a named setup and a named reason, and the rule can be tuned without touching the prompt.
- A new deny-list, validation rule, or filter applied at runtime to configuration already stored in installations is Red regardless of how common the setup is. A check that fires only when the value is saved again is Yellow.
- Before the model reads the change, tooling adds the code around each changed spot and the places that call it, taken from the tree before the change. No model call is involved.
- One review per pull request in production. The benchmark ran three to measure consistency.

## Planned action

- Runs on `pull_request` and appends the note to the PR description.
- Posts one comment per review carrying the bot version, the head and base SHA reviewed, and the verdict with its reason, and invites a thumbs up or thumbs down plus a reply saying why. Those reactions and replies are read back by a script to grade the bot per repository.
- Caches on the diff hash, so a re-push without changes costs nothing.
- Takes an OpenAI-compatible model endpoint and model name from secrets, and per-repository settings from a `.compatibility-review.toml` at the repository root.

## License

Apache-2.0. See `LICENSE`.
