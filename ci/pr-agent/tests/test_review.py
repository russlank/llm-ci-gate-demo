import copy
import unittest
from unittest.mock import Mock, patch

from jinja2 import Environment, StrictUndefined
from litellm import GoogleAIStudioGeminiConfig
from pr_agent.config_loader import get_settings

from pr_agent.algo.utils import convert_to_markdown_v2

from review import (
    UPSTREAM_GUIDANCE, configure_model, configure_review_prompt, render_review_markdown, render_rule_checks,
)
from settings import load_settings


class ModelSettingsTests(unittest.TestCase):
    def test_yaml_keys_are_supported_by_pinned_pr_agent(self):
        upstream = get_settings()
        for section, values in load_settings()["pr_agent"].items():
            for field in values:
                key = f"{section}.{field}"
                self.assertTrue(upstream.exists(key), key)

    def configured_prompt(self, rules):
        review = load_settings()["review"]
        settings = Mock()
        settings.get.return_value = get_settings().pr_review_prompt.system
        configure_review_prompt(settings, review, rules, "Approved policy text")
        return review, {call.args[0]: call.args[1] for call in settings.set.call_args_list}

    def test_prompt_requires_catalog_ids_and_allows_policy_findings(self):
        review, forwarded = self.configured_prompt({"ENG-004": "required"})
        prompt = forwarded["pr_review_prompt.system"]
        for text in review["guidance"].values():
            self.assertIn(text, prompt)
        self.assertIn("Include one entry for each rule marked violated", prompt)
        self.assertIn("        ENG-004", prompt)
        self.assertNotIn("One or two word title", prompt)
        self.assertNotIn("Possible Bug", prompt)
        self.assertIn("Approved policy text", forwarded["config.system_instructions"])

    def test_prompt_removes_upstream_bias_against_policy_findings(self):
        _, forwarded = self.configured_prompt({"ENG-004": "required"})
        prompt = forwarded["pr_review_prompt.system"]
        for upstream in UPSTREAM_GUIDANCE.values():
            self.assertNotIn(upstream, prompt)
        self.assertNotIn("prefer not reporting over guessing", prompt)
        self.assertNotIn("Use high only when the PR introduces a clear bug", prompt)

    def test_prompt_requests_a_rule_checklist_before_findings(self):
        _, forwarded = self.configured_prompt({"ENG-004": "required"})
        prompt = forwarded["pr_review_prompt.system"]
        self.assertIn("class RuleCheck(BaseModel):", prompt)
        self.assertLess(prompt.index("class RuleCheck"), prompt.index("class Review(BaseModel)"))
        schema_check = prompt.index("    rule_checks: List[RuleCheck]")
        self.assertLess(schema_check, prompt.index("    key_issues_to_review: List[KeyIssuesComponentLink]"))
        example_check = prompt.index("  rule_checks:\n")
        self.assertLess(example_check, prompt.index("  key_issues_to_review:\n"))
        self.assertIn("Complete rule_checks first", forwarded["pr_reviewer.extra_instructions"])

    def test_example_recommendation_matches_the_example_rule(self):
        for enforcement, risk, recommendation in (
            ("required", "high", "changes_required"), ("advisory", "medium", "no_concerns_found"),
        ):
            with self.subTest(enforcement=enforcement):
                _, forwarded = self.configured_prompt({"ENG-004": enforcement})
                prompt = forwarded["pr_review_prompt.system"]
                self.assertIn(f"\n  risk_level: {risk}\n", prompt)
                self.assertIn(f"\n  merge_recommendation: {recommendation}\n", prompt)

    def test_rendered_prompt_keeps_the_checklist_and_policy_fields(self):
        # Render through Jinja to catch template breakage from the inserted text.
        _, forwarded = self.configured_prompt({"ENG-004": "required"})
        rendered = Environment(undefined=StrictUndefined).from_string(forwarded["pr_review_prompt.system"]).render(
            num_max_findings=12, require_risk_assessment=True, require_merge_recommendation=True,
            require_tests=True, require_security_review=True, diff_hunk_format="", extra_instructions="",
            artifact_context=None, repo_context="", related_tickets=[], skills_context="",
            require_estimate_effort_to_review=False, require_score=False, require_can_be_split_review=False,
            require_todo_scan=False, require_failure_modes=False, require_priority_files=False,
            require_estimate_contribution_time_cost=False, question_str="", num_pr_files=1,
        )
        self.assertIn("rule_checks: List[RuleCheck]", rendered)
        self.assertIn("List up to 12 concrete violations", rendered)
        self.assertIn("Use changes_required when a required engineering rule is violated", rendered)

    def test_gemini_flash_lite_keeps_low_thinking_and_omits_temperature(self):
        original = GoogleAIStudioGeminiConfig.map_openai_params
        config = load_settings()
        with patch.object(GoogleAIStudioGeminiConfig, "map_openai_params", original):
            configure_model(Mock(), config, "gemini")
            params = GoogleAIStudioGeminiConfig().map_openai_params(
                non_default_params={"reasoning_effort": config["review"]["effort"]},
                optional_params={},
                model=config["providers"]["gemini"]["model"].removeprefix("gemini/"),
                drop_params=False,
            )

        self.assertNotIn("temperature", params)
        self.assertEqual(params["thinkingConfig"]["thinkingLevel"].lower(), config["review"]["effort"])

    def test_common_effort_and_timeout_follow_the_selected_profile(self):
        config = copy.deepcopy(load_settings())
        config["review"]["effort"] = "medium"
        config["timeout_seconds"]["model"] = 100
        for provider, timeout in (("claude", 100), ("openai", 75)):
            with self.subTest(provider=provider):
                settings = Mock()
                configure_model(settings, config, provider)
                forwarded = {call.args[0]: call.args[1] for call in settings.set.call_args_list}
                self.assertEqual(forwarded["config.ai_timeout"], timeout)
                if provider == "claude":
                    self.assertEqual(forwarded["config.reasoning_effort"], "medium")
                    self.assertTrue(forwarded["config.enable_claude_adaptive_thinking"])
                else:
                    self.assertNotIn("config.reasoning_effort", forwarded)


def block(value):
    """A value as PR-Agent parses a YAML block scalar: with a trailing newline."""
    return f"{value}\n"


# The verdicts from a real SC06 review comment, as the model returned them.
SC06_CHECKS = [
    {"rule_id": block(rule), "status": block(status), "evidence": block(evidence)}
    for rule, status, evidence in (
        ("ENG-001", "compliant", "Added constants and options use proper named declarations without magic strings."),
        ("ENG-002", "compliant", "GreetingService evaluates FeatureFlags.SignedGreeting before executing signed greeting behavior."),
        ("ENG-003", "compliant", "SignedGreeting is set to false in appsettings.json."),
        ("ENG-004", "compliant", "Feature flag evaluation uses FeatureFlags.SignedGreeting."),
        ("ENG-005", "violated", "tests/HelloWorld.Tests/GreetingServiceTests.cs only tests SignedGreeting\nwhen enabled is true."),
        ("ENG-006", "not_applicable", "No exception handling was added in the PR diff."),
        ("ENG-007", "not_applicable", "Program.cs was not modified in this PR."),
        ("ENG-008", "compliant", "Existing greeting behavior is preserved when features are disabled."),
        ("ENG-009", "compliant", "All new text and string templates are spelled correctly."),
    )
]


class RuleCheckRenderingTests(unittest.TestCase):
    def test_checklist_renders_as_one_compact_table(self):
        text = render_rule_checks(SC06_CHECKS)
        self.assertIn("<summary><strong>Rule checks</strong>: ❌ ENG-005 violated · 6 compliant · 2 not applicable</summary>", text)
        self.assertIn("| ENG-005 | ❌ violated | tests/HelloWorld.Tests/GreetingServiceTests.cs only tests "
                      "SignedGreeting when enabled is true. |", text)
        self.assertIn("| ENG-006 | ➖ not applicable | No exception handling was added in the PR diff. |", text)
        rows = [line for line in text.splitlines() if line.startswith("| ENG-")]
        self.assertEqual(len(rows), 9)
        self.assertNotIn("\n'", text)

    def test_untrusted_evidence_is_escaped_and_bounded(self):
        text = render_rule_checks([{"rule_id": "ENG-001", "status": "maybe",
                                    "evidence": "<img src=x> a|b\n" + "x" * 400}])
        row = next(line for line in text.splitlines() if line.startswith("| ENG-001"))
        self.assertIn("&lt;img src=x&gt; a&#124;b", row)
        self.assertIn("❔ maybe", row)
        self.assertTrue(row.endswith("… |"))
        self.assertEqual(row.count(" | "), 2)

    def test_wrapper_keeps_the_report_and_moves_the_checklist_to_a_table(self):
        report = {"review": {"risk_level": "low", "rule_checks": SC06_CHECKS, "key_issues_to_review": []}}
        rendered = render_review_markdown(convert_to_markdown_v2)(report, True)
        self.assertIn("| ENG-005 | ❌ violated |", rendered)
        self.assertNotIn("rule_id:", rendered)
        self.assertEqual(report["review"]["rule_checks"], SC06_CHECKS)

    def test_wrapper_leaves_reviews_without_a_checklist_unchanged(self):
        report = {"review": {"risk_level": "low", "key_issues_to_review": []}}
        self.assertEqual(render_review_markdown(convert_to_markdown_v2)(report, True), convert_to_markdown_v2(report, True))

