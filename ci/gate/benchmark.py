"""Record repeated reviews of one scenario and PR head as JSON Lines."""

import argparse
import json
import re
import subprocess

from evaluate import EXIT_CODES, RULE_ID, SHA
from settings import MODEL_KEY_VARIABLES, load_settings


def review(args):
    """Run one container and require its JSON outcome to match the exit code."""
    token = "PLUGIN_GITEA_TOKEN" if args.gitea_url else "PLUGIN_GITHUB_TOKEN"
    command = ["docker", "run", "--rm", "-e", token, "-e", MODEL_KEY_VARIABLES[args.llm_provider],
               "-e", f"PLUGIN_LLM_PROVIDER={args.llm_provider}",
               args.image, "--pr-url", args.pr_url, "--head", args.head]
    if args.gitea_url:
        command.extend(["--gitea-url", args.gitea_url])
    try:
        run = subprocess.run(
            command,
            capture_output=True, text=True, timeout=load_settings()["benchmark"]["timeout_seconds"], check=False,
        )
        report = json.loads(run.stdout)
        if not isinstance(report, dict) or EXIT_CODES.get(report.get("decision")) != run.returncode:
            raise ValueError("inconsistent gate result")
        return report
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {"decision": "ERROR", "reason": "benchmark_runner_failure"}


def main():
    """Validate the scenario inputs and emit one measurement per attempt."""
    settings = load_settings()
    benchmark = settings["benchmark"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario")
    parser.add_argument("pr_url")
    parser.add_argument("head", help="full SHA of the scenario commit")
    parser.add_argument("expected", choices=EXIT_CODES)
    parser.add_argument("--rule", action="append", default=[], help="expected rule ID, repeat as needed")
    parser.add_argument("--image", default=benchmark["image"])
    parser.add_argument("--llm-provider", choices=MODEL_KEY_VARIABLES, default=settings["default_provider"])
    parser.add_argument("--gitea-url", help="trusted Gitea instance URL")
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    args = parser.parse_args()
    if not re.fullmatch(r"SC(?:0[1-9]|1[0-6])", args.scenario):
        parser.error("expected a scenario ID from SC01 through SC16")
    if not SHA.fullmatch(args.head) or not all(RULE_ID.fullmatch(rule) for rule in args.rule):
        parser.error("expected a full head SHA and ENG-NNN rule IDs")

    for attempt in range(1, benchmark["attempts"] + 1):
        report = review(args)
        classified = report["decision"] in {"PASS", "WARNING", "FAIL"}
        found = set(report.get("required_rule_ids", [])) | set(report.get("advisory_rule_ids", []))
        expected_rules = set(args.rule)
        usage = report.get("usage", {})
        cost = None
        if (args.input_usd_per_million is not None and args.output_usd_per_million is not None
                and "prompt_tokens" in usage and "completion_tokens" in usage):
            cost = (usage["prompt_tokens"] * args.input_usd_per_million
                    + usage["completion_tokens"] * args.output_usd_per_million) / 1_000_000
        # An infrastructure or coverage failure supplies no classification to score.
        print(json.dumps({
            **report,
            "scenario": args.scenario,
            "attempt": attempt,
            "expected_head": args.head,
            "expected_decision": args.expected,
            "expected_rule_ids": sorted(expected_rules),
            "false_positive_rule_ids": sorted(found - expected_rules) if classified else None,
            "false_negative_rule_ids": sorted(expected_rules - found) if classified else None,
            "estimated_cost_usd": cost,
        }, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
