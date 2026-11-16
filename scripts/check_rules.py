import argparse
import json
import os
from pathlib import Path

from pairs import PAIRS_PATH, load_pairs
from pairs.validate import validate
from poller.rule_checker import OpenAIRuleReviewer, review_pair


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=Path, default=PAIRS_PATH)
    parser.add_argument("--out", type=Path, default=Path("data/derived/rule_reviews.jsonl"))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    args = parser.parse_args(argv)
    
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        parser.error("Set OPENAI_API_KEY in your environment (do not put it in source code)")
    if not args.model:
        parser.error("Set OPENAI_MODEL or pass --model")
    if args.out.resolve() == args.pairs.resolve():
        parser.error("Review output cannot overwrite pairs.json")
    pairs = load_pairs(args.pairs, active_only=False)
    errors, _ = validate(pairs)
    if errors:
        parser.error("Invalid pairs file: " + "; ".join(errors))
    reviewer = OpenAIRuleReviewer(api_key, args.model)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    failed = 0

    with args.out.open("a", encoding="utf-8") as output:
        for pair in pairs:
            result = review_pair(pair, reviewer)
            output.write(json.dumps(result, sort_keys=True) + "\n")
            output.flush()
            failed += result["status"] == "failed"
            print(f"{pair['id']}: {result['verdict']} ({result['confidence']:.0%}); admin pending")
    print(f"Reviewed {len(pairs)} tracked pairs; {failed} failed. Report: {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
