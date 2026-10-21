"""Review one change in a git checkout.

    compatibility-review --repo . --base <ref> --head <ref> --model <name> [options]

The diff is taken from the merge base of --base and --head to --head, and the repository facts
from the tree at that merge base (the code before the change). `--dry-run` prints the assembled
input and makes no model call.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import enrich, review, scope
from .change import build_input, diff_from_merge_base, extract_tree


def main(argv=None):
    ap = argparse.ArgumentParser(prog="compatibility-review", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="path to the git checkout")
    ap.add_argument("--base", required=True, help="base ref of the change (the target branch)")
    ap.add_argument("--head", default="HEAD", help="head ref of the change")
    ap.add_argument("--title", default="", help="pull request title")
    ap.add_argument("--branch", help="target branch name shown to the model (default: --base)")
    ap.add_argument("--body-file", help="file holding the author's description")
    ap.add_argument("--model", default=os.environ.get("COMPAT_REVIEW_MODEL"), help="model name at the endpoint")
    ap.add_argument("--api", choices=sorted(review.CALLS), default=os.environ.get("COMPAT_REVIEW_API", "responses"))
    ap.add_argument("--base-url", help="endpoint base URL (default: OPENAI_BASE_URL / ANTHROPIC_BASE_URL)")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--no-enrich", action="store_true", help="send the diff without repository facts")
    ap.add_argument("--dry-run", action="store_true", help="print the assembled input; no model call")
    ap.add_argument("--out", help="write the result JSON here as well as to stdout")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--attempts", type=int, default=4)
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        cfg = scope.load(repo)
        merge_base, head_sha, diff = diff_from_merge_base(repo, args.base, args.head)
    except RuntimeError as e:
        ap.exit(2, f"{e}\n")
    body = Path(args.body_file).read_text() if args.body_file else ""
    branch = args.branch or args.base

    skipped = scope.out_of_scope(diff, cfg.get("ignore", []))
    if skipped and not args.dry_run:
        return emit(scope.skipped_result(skipped), args.out)

    facts = None
    if not args.no_enrich:
        with tempfile.TemporaryDirectory(prefix="compat-review-") as tmp:
            extract_tree(repo, merge_base, tmp)
            facts = enrich.facts(diff, tmp, lambda p: scope.is_test(p, cfg.get("ignore", [])))
    user = build_input(branch, args.title, body, diff, facts)

    if args.dry_run:
        sys.stdout.write(user)
        return 0
    if not args.model:
        ap.error("--model (or COMPAT_REVIEW_MODEL) is required")
    result = review.review(user, scope.context_text(repo, cfg), args.model, args.api, args.base_url,
                           args.effort, enriched=facts is not None, timeout=args.timeout,
                           attempts=args.attempts)
    result["_meta"].update({"base": merge_base, "head": head_sha})
    return emit(result, args.out)


def emit(result, out):
    text = json.dumps(result, indent=1)
    if out:
        Path(out).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
