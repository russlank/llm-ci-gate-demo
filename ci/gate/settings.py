"""Load and validate the operational settings installed beside this module.

The plugin and reviewer use the image copy. The host benchmark uses its
checkout's copy for benchmark defaults. Credentials enter through the mapped
environment variables.
"""

from functools import lru_cache
from pathlib import Path

import yaml

# Each plugin setting maps to exactly one credential expected by PR-Agent.
MODEL_KEY_VARIABLES = {
    "openai": "PLUGIN_OPENAI_KEY",
    "gemini": "PLUGIN_GEMINI_KEY",
    "claude": "PLUGIN_CLAUDE_KEY",
}
PR_AGENT_KEY_VARIABLES = {
    "openai": "OPENAI__KEY",
    "gemini": "GOOGLE_AI_STUDIO__GEMINI_API_KEY",
    "claude": "ANTHROPIC__KEY",
}
REASONING_PROFILES = {
    "openai": {"none", "openai_reasoning_effort"},
    "gemini": {"gemini_thinking_level"},
    "claude": {"claude_adaptive"},
}
MODEL_PREFIXES = {"openai": "openai", "gemini": "gemini", "claude": "anthropic"}
SETTINGS_FILE = Path(__file__).with_name("settings.yaml")
# Prompt texts the reviewer substitutes into the pinned upstream review prompt.
GUIDANCE_KEYS = frozenset({"finding", "certainty", "uncertainty", "comments"})
FIELD_DESCRIPTION_KEYS = frozenset({
    "issue_header", "issue_content", "key_issues_to_review", "risk_level", "merge_recommendation",
})
RULE_CHECK_KEYS = frozenset({"description", "status", "evidence"})


class SettingsError(ValueError):
    """The installed settings file is missing or invalid."""


def positive_int(value, name):
    """Reject zero, negative values, and YAML booleans in numeric settings."""
    if type(value) is not int or value <= 0:
        raise SettingsError(f"{name} must be a positive integer")


@lru_cache(maxsize=1)
def load_settings(path=SETTINGS_FILE):
    """Parse and validate the YAML once per process; callers treat it as read-only."""
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        for field in ("changed_files", "changed_lines", "git_patch_bytes", "json_bytes", "review_log_chars",
                      "inline_comment_chars", "inline_review_pages"):
            positive_int(data["limits"][field], f"limits.{field}")
        timeouts = data["timeout_seconds"]
        for field in ("forge", "git", "model", "reviewer"):
            positive_int(timeouts[field], f"timeout_seconds.{field}")
        benchmark = data["benchmark"]
        for field in ("timeout_seconds", "attempts"):
            positive_int(benchmark[field], f"benchmark.{field}")
        if not timeouts["reviewer"] < benchmark["timeout_seconds"]:
            raise SettingsError("Benchmark timeout must exceed the reviewer timeout")
        if set(data["providers"]) != set(MODEL_KEY_VARIABLES) or data["default_provider"] not in data["providers"]:
            raise SettingsError("The provider list or default provider is invalid")
        for name, profile in data["providers"].items():
            model = profile["model"]
            if not isinstance(model, str) or not model or any(c.isspace() for c in model):
                raise SettingsError(f"providers.{name}.model must be a model identifier")
            # Match the configured provider to the SDK route receiving its key.
            if (name != "openai" or "/" in model) and not model.startswith(f"{MODEL_PREFIXES[name]}/"):
                raise SettingsError(f"providers.{name}.model has the wrong provider prefix")
            if profile["reasoning"] not in REASONING_PROFILES[name]:
                raise SettingsError(f"providers.{name}.reasoning is unsupported")
            timeout = model_timeout(data, name)
            positive_int(timeout, f"providers.{name} model timeout")
            if timeout >= timeouts["reviewer"]:
                raise SettingsError("Model timeout must be less than the reviewer timeout")
        review = data["review"]
        if review["effort"] not in ("low", "medium", "high"):
            raise SettingsError("review.effort must be low, medium, or high")
        if type(review["temperature"]) not in (int, float) or not 0.0 <= review["temperature"] <= 2.0:
            raise SettingsError("review.temperature must be between 0 and 2")
        prompt_texts = {
            "guidance": (review["guidance"], GUIDANCE_KEYS),
            "field_descriptions": (review["field_descriptions"], FIELD_DESCRIPTION_KEYS),
            "rule_checks": (review["rule_checks"], RULE_CHECK_KEYS),
        }
        for name, (values, keys) in prompt_texts.items():
            if not isinstance(values, dict) or set(values) != keys:
                raise SettingsError(f"review.{name} must define exactly {', '.join(sorted(keys))}")
            if not all(isinstance(value, str) and value.strip() for value in values.values()):
                raise SettingsError(f"review.{name} values must be nonempty strings")
            # These texts are inserted into the upstream Jinja template.
            if any(marker in value for value in values.values() for marker in ("{%", "{#")):
                raise SettingsError(f"review.{name} must not contain Jinja statements or comments")
        if not isinstance(review["example_rule_id"], str) or not review["example_rule_id"].strip():
            raise SettingsError("review.example_rule_id must be a nonempty string")
        for value in (
            review["log_output"], review["inline_comments"],
            *(data["forges"][name]["persistent_comment"] for name in ("github", "gitea")),
        ):
            if type(value) is not bool:
                raise SettingsError("Review logging and comment options must be booleans")
        if data["pr_agent"]["pr_reviewer"].get("inline_key_issues", False):
            raise SettingsError("Set review.inline_comments to publish validated inline findings")
        for value in (data["target_branch"], benchmark["image"], review["policy_preamble"], review["additional_instructions"]):
            if not isinstance(value, str) or not value.strip():
                raise SettingsError("Branch, benchmark image, and review instructions must be nonempty strings")
        positive_int(data["pr_agent"]["pr_reviewer"]["num_max_findings"], "pr_agent.pr_reviewer.num_max_findings")
        positive_int(data["pr_agent"]["config"]["max_model_tokens"], "pr_agent.config.max_model_tokens")
        max_output = data["pr_agent"]["config"]["max_output_tokens"]
        if type(max_output) is not int or max_output < 0:
            raise SettingsError("pr_agent.config.max_output_tokens must be a nonnegative integer")
        if not all(isinstance(values, dict) for values in data["pr_agent"].values()):
            raise SettingsError("Each PR-Agent settings section must be a mapping")
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, AttributeError) as error:
        raise SettingsError("Check the installed ci/gate/settings.yaml") from error
    return data


def model_timeout(settings, provider):
    """Resolve the provider override, falling back to the shared request budget."""
    return settings["providers"][provider].get("request_timeout_seconds", settings["timeout_seconds"]["model"])
