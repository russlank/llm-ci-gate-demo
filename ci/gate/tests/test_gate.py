import copy
import json
import unittest
from argparse import Namespace
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import run
from evaluate import Decision, ReviewExit, evaluate, load_policy

HEAD = "a" * 40
BASE = "b" * 40
IDENTITY = {"head_sha": HEAD, "base_sha": BASE}
PATH = "src/HelloWorld/GreetingService.cs"
SNAPSHOT = {**IDENTITY, "changed_files": [PATH], "review_files": [PATH]}
PR_URL = "https://github.com/example/demo/pull/1"
GITEA_URL = "https://forge.example.test/git"
FIXTURE = Path(__file__).with_name("clean-review.json")


def finding(identifier):
    return {
        "issue_header": identifier,
        "relevant_file": PATH,
        "issue_content": "Synthetic evidence and correction.",
        "start_line": 10,
        "end_line": 10,
    }


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(FIXTURE.read_text())
        self.rules, _, _ = load_policy(run.ROOT / "policies")

    def decide(self, snapshot=SNAPSHOT, max_findings=12):
        return evaluate(self.report, snapshot, IDENTITY, self.rules, max_findings)

    def test_complete_review_passes(self):
        self.assertEqual(self.decide(), Decision("PASS", "complete_review"))

    def test_yaml_boolean_answers_and_display_labels_are_accepted(self):
        self.report["review"].update(
            relevant_tests=True, security_concerns=False, risk_level="Low",
            merge_recommendation="No concerns found",
        )
        self.assertEqual(self.decide(), Decision("PASS", "complete_review"))

    def test_required_rule_fails_even_with_clean_recommendation(self):
        self.report["review"]["key_issues_to_review"] = [finding("ENG-007"), finding("ENG-001")]
        self.assertEqual(self.decide(), Decision("FAIL", "required_findings", ("ENG-001",), ("ENG-007",)))

    def test_reported_misspelling_in_printed_text_blocks(self):
        self.report["review"]["key_issues_to_review"] = [{
            **finding("ENG-009"), "issue_content": 'The greeting prints "Hlelo". Change it to "Hello".',
        }]
        self.assertEqual(self.decide(), Decision("FAIL", "required_findings", ("ENG-009",)))

    def test_block_scalar_finding_with_trailing_newlines_fails(self):
        # The prompt asks for YAML block scalars, which keep a trailing newline.
        self.report["review"]["key_issues_to_review"] = [{
            **finding("ENG-004"), "issue_header": "ENG-004\n", "relevant_file": f"{PATH}\n",
        }]
        self.assertEqual(self.decide(), Decision("FAIL", "required_findings", ("ENG-004",)))

    def test_required_finding_fails_without_rule_checks(self):
        del self.report["review"]["rule_checks"]
        self.report["review"]["key_issues_to_review"] = [finding("ENG-004")]
        self.assertEqual(self.decide(), Decision("FAIL", "required_findings", ("ENG-004",)))

    def test_empty_review_without_complete_rule_checks_blocks(self):
        checks = self.report["review"]["rule_checks"]
        cases = {
            "missing": None,
            "empty": [],
            "one rule omitted": checks[1:],
            "duplicate rule": [*checks, checks[0]],
            "unknown status": [{**checks[0], "status": "maybe"}, *checks[1:]],
            "missing evidence": [{**checks[0], "evidence": " "}, *checks[1:]],
            "unknown rule": [{**checks[0], "rule_id": "ENG-999"}, *checks[1:]],
        }
        for name, value in cases.items():
            with self.subTest(name):
                if value is None:
                    self.report["review"].pop("rule_checks", None)
                else:
                    self.report["review"]["rule_checks"] = value
                self.assertEqual(self.decide(), Decision(
                    "REVIEW_REQUIRED", "incomplete_rule_checks", invalid_fields=("rule_checks",),
                ))

    def test_block_scalar_rule_checks_are_accepted(self):
        for check in self.report["review"]["rule_checks"]:
            check.update(rule_id=f"{check['rule_id']}\n", status=f"{check['status']}\n")
        self.assertEqual(self.decide(), Decision("PASS", "complete_review"))

    def test_violated_rule_check_without_finding_blocks(self):
        self.report["review"]["rule_checks"][8]["status"] = "violated"
        self.assertEqual(self.decide(), Decision(
            "REVIEW_REQUIRED", "unreported_rule_violations", invalid_fields=("rule_checks",),
        ))

    def test_advisory_only_warns(self):
        self.report["review"]["key_issues_to_review"] = [finding("ENG-007")]
        self.assertEqual(self.decide(), Decision("WARNING", "advisory_findings", advisory=("ENG-007",)))

    def test_ambiguous_findings_block(self):
        for evidence in (finding("ENG-999"), finding("ENG-001 and ENG-002"), "invalid", {**finding("ENG-001"), "start_line": True}):
            with self.subTest(evidence=evidence):
                self.report["review"]["key_issues_to_review"] = [evidence]
                self.assertEqual(self.decide().exit_code, 2)

    def test_findings_at_report_cap_block(self):
        self.report["review"]["key_issues_to_review"] = [finding("ENG-007")]
        self.assertEqual(self.decide(max_findings=1).exit_code, 2)

    def test_review_escalation_blocks(self):
        self.report["review"]["merge_recommendation"] = "needs_review"
        self.assertEqual(self.decide().exit_code, 2)

    def test_security_escalation_blocks(self):
        self.report["review"]["security_concerns"] = "Synthetic concern"
        self.assertEqual(self.decide().exit_code, 2)

    def test_missing_review_fields_block(self):
        del self.report["review"]["key_issues_to_review"]
        self.assertEqual(self.decide(), Decision(
            "REVIEW_REQUIRED", "missing_findings", invalid_fields=("key_issues_to_review",),
        ))

    def test_unsupported_review_values_name_invalid_fields(self):
        self.report["review"].update(relevant_tests=1, security_concerns=True, risk_level="unknown")
        self.assertEqual(self.decide(), Decision(
            "REVIEW_REQUIRED", "incomplete_review_fields",
            invalid_fields=("risk_level", "relevant_tests", "security_concerns"),
        ))

    def test_missing_or_partial_coverage_blocks(self):
        original = self.report.pop("coverage")
        self.assertEqual(self.decide().exit_code, 2)
        self.report["coverage"] = {**original, "files_partially_reviewed": [PATH]}
        self.assertEqual(self.decide().exit_code, 2)

    def test_missing_file_and_duplicate_file_block(self):
        self.assertEqual(self.decide({**SNAPSHOT, "review_files": []}).exit_code, 2)
        self.assertEqual(self.decide({**SNAPSHOT, "changed_files": [PATH, PATH]}).exit_code, 2)

    def test_unbound_head_or_base_blocks(self):
        for key in IDENTITY:
            self.assertEqual(self.decide({**SNAPSHOT, key: "c" * 40}).exit_code, 2)

    def test_malformed_report_errors(self):
        self.report = []
        self.assertEqual(self.decide().exit_code, 3)


def pr_metadata(head=HEAD, base=BASE):
    repository = {"full_name": "example/demo"}
    return {
        "state": "open", "number": 1,
        "head": {"repo": repository, "sha": head},
        "base": {"repo": repository, "sha": base, "ref": "main"},
    }


class RunnerTests(unittest.TestCase):
    def test_review_details_redact_credentials_and_escape_controls(self):
        messages = []
        run.log_review_data(messages.append, "Model output", "line\n\x1btoken-value", ("token-value",))
        self.assertEqual(messages, ['Model output "line\\n\\u001b[REDACTED]"'])

    @patch.object(run, "forge_json")
    def test_inline_review_uses_only_added_lines_on_the_reviewed_head(self, forge_json):
        report = json.loads(FIXTURE.read_text())
        # Block-scalar values with trailing newlines must still anchor to the trimmed path.
        report["review"]["key_issues_to_review"] = [
            {**finding("ENG-001"), "issue_header": "ENG-001\n", "relevant_file": f"{PATH}\n"},
            finding("ENG-999"), {**finding("ENG-007"), "start_line": 15, "end_line": 15},
        ]
        snapshot = {**SNAPSHOT, "added_lines": {PATH: [10]}}
        rules = {"ENG-001": "required", "ENG-007": "advisory"}
        settings = run.load_settings()
        for forge, anchor in (("gitea", {"new_position": 10, "old_position": 0}),
                              ("github", {"line": 10, "side": "RIGHT"})):
            with self.subTest(forge=forge):
                target = run.PullTarget(forge, GITEA_URL, "example/demo", "1", HEAD)
                forge_json.reset_mock()
                forge_json.side_effect = [[], {}]
                args = (target, "forge-token", "model-key", IDENTITY, report, snapshot, rules, settings)
                self.assertEqual(run.publish_inline_findings(*args), 1)
                payload = forge_json.call_args.args[-1]
                self.assertEqual(payload["commit_id"], HEAD)
                self.assertEqual(payload["event"], "COMMENT")
                self.assertEqual(payload["comments"], [{
                    "path": PATH, "body": "**ENG-001**\n\nSynthetic evidence and correction.", **anchor,
                }])
                forge_json.reset_mock()
                forge_json.side_effect = [[{"commit_id": HEAD, "body": payload["body"]}]]
                self.assertEqual(run.publish_inline_findings(*args), 0)
                self.assertEqual(forge_json.call_count, 1)

    @patch.object(run.subprocess, "run")
    def test_reviewer_exit_is_translated_to_a_fixed_reason(self, spawn):
        target = run.PullTarget("gitea", GITEA_URL, "example/demo", "1", HEAD)
        cases = (
            ("openai", ReviewExit.LLM_QUOTA_EXHAUSTED, Decision("ERROR", "openai_quota_exhausted")),
            ("gemini", ReviewExit.LLM_RATE_LIMITED, Decision("ERROR", "gemini_rate_limited")),
            ("openai", ReviewExit.GITEA_GIT_FETCH_FAILED, Decision("ERROR", "gitea_git_fetch_failed")),
            ("gemini", ReviewExit.INCOMPLETE_PR_INPUT, Decision("REVIEW_REQUIRED", "incomplete_pr_input")),
            ("openai", 1, Decision("ERROR", "pr_agent_failed")),
        )
        for provider, exit_code, expected in cases:
            with self.subTest(provider=provider, exit_code=exit_code):
                spawn.return_value.returncode = exit_code
                decision, _ = run.run_pr_agent(target, IDENTITY, Path("/unused"), "test-token", provider, "test-key")
                self.assertEqual(decision, expected)
                self.assertEqual(spawn.call_args.kwargs["stderr"], run.subprocess.DEVNULL)

    @patch.object(run.subprocess, "run")
    def test_reviewer_receives_only_the_selected_model_key(self, spawn):
        target = run.PullTarget("gitea", GITEA_URL, "example/demo", "1", HEAD)
        spawn.return_value.returncode = 1
        for provider, variable in run.PR_AGENT_KEY_VARIABLES.items():
            with self.subTest(provider=provider):
                run.run_pr_agent(target, IDENTITY, Path("/unused"), "test-token", provider, "test-key")
                child_env = spawn.call_args.kwargs["env"]
                keys = {name: child_env[name] for name in run.PR_AGENT_KEY_VARIABLES.values() if name in child_env}
                self.assertEqual(keys, {variable: "test-key"})
                self.assertEqual(spawn.call_args.args[0][-1], provider)

    @patch.object(run.subprocess, "run")
    def test_gemini_http_failure_emits_only_allowed_status_fields(self, spawn):
        target = run.PullTarget("gitea", GITEA_URL, "example/demo", "1", HEAD)
        spawn.return_value.returncode = ReviewExit.LLM_REQUEST_FAILED
        with TemporaryDirectory() as temp:
            work = Path(temp)
            path = work / "model-error.json"
            for error_status, expected in (
                ("INVALID_ARGUMENT", {"model_http_status": 400, "model_error_status": "INVALID_ARGUMENT"}),
                ("private provider text", {"model_http_status": 400}),
                ({"unexpected": "private provider text"}, {"model_http_status": 400}),
            ):
                with self.subTest(error_status=error_status):
                    path.write_text(json.dumps({
                        "model_http_status": 400,
                        "model_error_status": error_status,
                        "message": "private provider text",
                    }))
                    decision, details = run.run_pr_agent(
                        target, IDENTITY, work, "test-token", "gemini", "test-key",
                    )
                    self.assertEqual(decision, Decision("ERROR", "gemini_request_failed"))
                    self.assertEqual(details, expected)

    @patch.object(run.subprocess, "run")
    def test_gemini_timeout_emits_only_a_known_phase(self, spawn):
        target = run.PullTarget("gitea", GITEA_URL, "example/demo", "1", HEAD)
        spawn.return_value.returncode = ReviewExit.LLM_TIMED_OUT
        with TemporaryDirectory() as temp:
            work = Path(temp)
            path = work / "model-error.json"
            for phase, expected in (("read", {"model_timeout_phase": "read"}), ("private error text", {})):
                with self.subTest(phase=phase):
                    path.write_text(json.dumps({"model_timeout_phase": phase}))
                    decision, details = run.run_pr_agent(
                        target, IDENTITY, work, "test-token", "gemini", "test-key",
                    )
                    self.assertEqual(decision, Decision("ERROR", "gemini_timed_out"))
                    self.assertEqual(details, expected)

    @patch.dict(run.os.environ, {"CI_FORGE_TYPE": "gitlab"}, clear=True)
    @patch.object(run, "pull_request")
    def test_unsupported_forge_cannot_call_a_pr_api(self, lookup):
        result = run.review_pr(Namespace(pr_url=None, head=None, gitea_url=None), {})
        self.assertEqual(result, Decision("ERROR", "unsupported_forge"))
        lookup.assert_not_called()

    @patch.dict(run.os.environ, {
        "CI_FORGE_TYPE": "gitea", "CI_FORGE_URL": GITEA_URL, "CI_REPO": "example/demo",
        "CI_COMMIT_PULL_REQUEST": "1", "CI_COMMIT_SHA": HEAD, "CI_COMMIT_TARGET_BRANCH": "main",
        "CI_PIPELINE_EVENT": "pull_request", "PLUGIN_GITEA_TOKEN": "local-test-token", "PLUGIN_OPENAI_KEY": "test",
    }, clear=True)
    @patch.object(run, "pull_request", return_value=pr_metadata())
    @patch.object(run, "run_pr_agent", return_value=(json.loads(FIXTURE.read_text()), SNAPSHOT))
    def test_gitea_pipeline_uses_its_own_endpoint_and_token(self, reviewer, lookup):
        details = {}
        result = run.review_pr(Namespace(pr_url=None, head=None, gitea_url=None), details)
        self.assertEqual(result.outcome, "PASS")
        self.assertEqual(details["forge"], "gitea")
        self.assertEqual(details["llm_provider"], "openai")
        self.assertEqual(lookup.call_count, 2)
        target, token = lookup.call_args.args
        self.assertEqual(target.pr_url, f"{GITEA_URL}/example/demo/pulls/1")
        self.assertEqual(token, "local-test-token")
        self.assertEqual(reviewer.call_args.args[0], target)

    @patch.dict(run.os.environ, {
        "PLUGIN_GITEA_TOKEN": "test-token", "PLUGIN_LLM_PROVIDER": "gemini", "PLUGIN_GEMINI_KEY": "test-key",
    }, clear=True)
    @patch.object(run, "pull_request", return_value=pr_metadata())
    @patch.object(run, "run_pr_agent", return_value=(json.loads(FIXTURE.read_text()), SNAPSHOT))
    def test_gemini_review_reports_the_selected_model(self, reviewer, lookup):
        details = {}
        args = Namespace(pr_url=f"{GITEA_URL}/example/demo/pulls/1", head=HEAD, gitea_url=GITEA_URL)
        self.assertEqual(run.review_pr(args, details).outcome, "PASS")
        self.assertEqual(details["llm_provider"], "gemini")
        self.assertEqual(details["model"], run.load_settings()["providers"]["gemini"]["model"])
        self.assertEqual(reviewer.call_args.args[-2:], ("gemini", "test-key"))

    @patch.dict(run.os.environ, {
        "PLUGIN_GITHUB_TOKEN": "test", "PLUGIN_OPENAI_KEY": "unused", "PLUGIN_LLM_PROVIDER": "gemini",
    }, clear=True)
    @patch.object(run, "pull_request")
    def test_gemini_requires_its_own_key(self, lookup):
        args = Namespace(pr_url=PR_URL, head=HEAD, gitea_url=None)
        self.assertEqual(run.review_pr(args, {}).reason, "missing_credentials")
        lookup.assert_not_called()

    @patch.dict(run.os.environ, {"PLUGIN_LLM_PROVIDER": "other"}, clear=True)
    @patch.object(run, "pull_request")
    def test_unsupported_model_provider_cannot_start_review(self, lookup):
        args = Namespace(pr_url=PR_URL, head=HEAD, gitea_url=None)
        self.assertEqual(run.review_pr(args, {}).reason, "unsupported_llm_provider")
        lookup.assert_not_called()

    @patch.object(run, "build_opener")
    def test_gitea_pr_uses_api_path_and_token_header(self, opener):
        target = run.PullTarget("gitea", GITEA_URL, "example/demo", "1", HEAD)
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps(pr_metadata()).encode()
        self.assertEqual(run.pull_request(target, "test-token"), pr_metadata())
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, f"{GITEA_URL}/api/v1/repos/example/demo/pulls/1")
        self.assertEqual(request.get_header("Authorization"), "token test-token")

    @patch.object(run, "pull_request")
    def test_manual_gitea_review_rejects_a_different_host(self, lookup):
        args = Namespace(pr_url="https://other.example.test/example/demo/pulls/1", head=HEAD, gitea_url=GITEA_URL)
        self.assertEqual(run.review_pr(args, {}).reason, "invalid_pr_metadata")
        lookup.assert_not_called()

    @patch.dict(run.os.environ, {"PLUGIN_GITHUB_TOKEN": "test", "PLUGIN_OPENAI_KEY": "test"}, clear=True)
    @patch.object(run.sys, "argv", ["run.py", "--pr-url", PR_URL])
    @patch.object(run, "pull_request")
    def test_github_lookup_errors_are_sanitized(self, github):
        for error, reason, status in (
            (HTTPError(PR_URL, 404, "private message", {}, BytesIO(b"private body")), "forge_pr_lookup_http_error", 404),
            (URLError("private network detail"), "forge_pr_lookup_network_error", None),
        ):
            with (self.subTest(reason=reason),
                  patch("sys.stdout", new_callable=StringIO) as output,
                  patch("sys.stderr", new_callable=StringIO) as progress):
                github.side_effect = error
                self.assertEqual(run.main(), 3)
                report = json.loads(output.getvalue())
                self.assertEqual(report["reason"], reason)
                self.assertEqual(report.get("forge_http_status"), status)
                self.assertNotIn("private", output.getvalue())
                self.assertNotIn("[semantic-review]", output.getvalue())
                self.assertIn(f"Finished with ERROR ({reason})", progress.getvalue())
                self.assertNotIn("private", progress.getvalue())

    @patch.dict(run.os.environ, {"PLUGIN_GITHUB_TOKEN": "test", "PLUGIN_OPENAI_KEY": "test"}, clear=True)
    @patch.object(run, "pull_request")
    @patch.object(run, "run_pr_agent")
    def test_head_moving_during_run_blocks(self, reviewer, github):
        github.side_effect = [pr_metadata(), pr_metadata(head="c" * 40)]
        reviewer.return_value = json.loads(FIXTURE.read_text()), SNAPSHOT
        result = run.review_pr(Namespace(pr_url=PR_URL, head=HEAD, gitea_url=None), {})
        self.assertEqual(result, Decision("REVIEW_REQUIRED", "pr_identity_changed"))

    @patch.dict(run.os.environ, {"PLUGIN_GITHUB_TOKEN": "test", "PLUGIN_OPENAI_KEY": "test"}, clear=True)
    @patch.object(run, "pull_request", return_value=pr_metadata())
    @patch.object(run, "run_pr_agent")
    def test_old_pipeline_never_calls_reviewer(self, reviewer, github):
        result = run.review_pr(Namespace(pr_url=PR_URL, head="c" * 40, gitea_url=None), {})
        self.assertEqual(result, Decision("REVIEW_REQUIRED", "pipeline_head_is_stale"))
        reviewer.assert_not_called()

    @patch.dict(run.os.environ, {"PLUGIN_GITHUB_TOKEN": "test", "PLUGIN_OPENAI_KEY": "test"}, clear=True)
    @patch.object(run, "pull_request", return_value=pr_metadata())
    @patch.object(run, "run_pr_agent", return_value=(Decision("ERROR", "pr_agent_failed"), {}))
    def test_provider_failure_errors(self, reviewer, github):
        result = run.review_pr(Namespace(pr_url=PR_URL, head=HEAD, gitea_url=None), {})
        self.assertEqual(result.exit_code, 3)

    def test_fork_rejected(self):
        fork = copy.deepcopy(pr_metadata())
        fork["head"]["repo"] = {"full_name": "somebody/fork"}
        with self.assertRaises(ValueError):
            run.identity(fork, "example/demo", 1)


if __name__ == "__main__":
    unittest.main()
