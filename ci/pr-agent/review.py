"""Run the upstream CLI with a commit-pinned comparison from the selected forge."""

import html
import json
import os
import re
import sys
from functools import partial
from importlib.metadata import version
from pathlib import Path

import httpx
import openai
from giteapy.rest import ApiException
from litellm import GoogleAIStudioGeminiConfig
from loguru import logger
from pr_agent.algo.token_budget import FallbackEligibleError
from pr_agent.cli import run
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import register_git_provider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools import pr_reviewer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci" / "gate"))
from evaluate import GEMINI_ERROR_STATUSES, PR_AGENT_VERSION, ReviewExit, load_policy  # noqa: E402
from gitea_compare import (  # noqa: E402
    GiteaCommentError, GiteaGitFetchError, IncompleteInput, added_line_numbers, create_gitea_provider,
    validate_comparison_size,
)
from run import log_progress, log_review_data, review_logging_enabled  # noqa: E402
from settings import PR_AGENT_KEY_VARIABLES, SettingsError, load_settings, model_timeout  # noqa: E402


MODEL_STATUS_FAILURES = {
    401: ReviewExit.LLM_AUTHENTICATION_FAILED,
    403: ReviewExit.LLM_ACCESS_DENIED,
    404: ReviewExit.LLM_MODEL_UNAVAILABLE,
    429: ReviewExit.LLM_RATE_LIMITED,
}
MODEL_API_HOSTS = {
    "openai": "api.openai.com",
    "gemini": "generativelanguage.googleapis.com",
    "claude": "api.anthropic.com",
}
TIMEOUT_PHASE_BY_TYPE = {
    httpx.ConnectTimeout: "connect",
    httpx.ReadTimeout: "read",
    httpx.WriteTimeout: "write",
    httpx.PoolTimeout: "pool",
}
COMPLETION_LOG_SOURCE = ("pr_agent.algo.ai_handlers.litellm_ai_handler", "_chat_completion_with_retry")
MODEL_OUTPUT_PREFIX = "\nAI response:\n"


def classify_failure(error):
    """Unwrap reviewer failures into fixed codes without publishing exception text."""
    for _ in range(8):
        if isinstance(error, GiteaGitFetchError):
            return ReviewExit.GITEA_GIT_FETCH_FAILED
        if isinstance(error, GiteaCommentError):
            return ReviewExit.GITEA_COMMENT_FAILED
        if isinstance(error, ApiException):
            return ReviewExit.GITEA_API_ERROR
        if isinstance(error, openai.APITimeoutError):
            return ReviewExit.LLM_TIMED_OUT
        if isinstance(error, openai.APIConnectionError):
            return ReviewExit.LLM_CONNECTION_FAILED
        if isinstance(error, openai.APIStatusError):
            if error.code == "insufficient_quota":
                return ReviewExit.LLM_QUOTA_EXHAUSTED
            return MODEL_STATUS_FAILURES.get(error.status_code, ReviewExit.LLM_REQUEST_FAILED)
        if isinstance(error, FallbackEligibleError):
            return ReviewExit.PR_AGENT_RESPONSE_INVALID
        if isinstance(error, openai.APIError):
            return ReviewExit.LLM_REQUEST_FAILED
        if error is None:
            break
        error = error.__cause__ or error.__context__
    return None


def model_failure_details(error, provider):
    """Extract an allowlisted detail from the selected model transport's exception chain."""
    for _ in range(8):
        if error is None:
            break
        if phase := TIMEOUT_PHASE_BY_TYPE.get(type(error)):
            return {"model_timeout_phase": phase}
        if (isinstance(error, httpx.HTTPStatusError)
                and error.request.url.host == MODEL_API_HOSTS[provider]
                and 400 <= error.response.status_code <= 599):
            details = {"model_http_status": error.response.status_code}
            if provider != "gemini":
                return details
            try:
                status = error.response.json()["error"]["status"]
            except (httpx.ResponseNotRead, KeyError, TypeError, ValueError):
                return details
            if isinstance(status, str) and status in GEMINI_ERROR_STATUSES:
                details["model_error_status"] = status
            return details
        error = error.__cause__ or error.__context__
    return {}


def complete_patch(file):
    """Check that GitHub's patch contains every addition and deletion it reported."""
    patch = file.patch
    if not isinstance(patch, str):
        return False
    lines = patch.split("\n")
    return (sum(line.startswith("+") for line in lines) == file.additions
            and sum(line.startswith("-") for line in lines) == file.deletions
            and file.additions + file.deletions > 0)


def omit_gemini_temperature():
    """Remove LiteLLM's default sampler setting in the Gemini reviewer process."""
    # The pinned LiteLLM adds a temperature even when PR-Agent omits it.
    # The selected Gemini model deprecates that field. Strip it after mapping.
    map_params = GoogleAIStudioGeminiConfig.map_openai_params

    def map_without_temperature(self, non_default_params, optional_params, model, drop_params):
        params = map_params(self, non_default_params, optional_params, model, drop_params)
        params.pop("temperature", None)
        return params

    GoogleAIStudioGeminiConfig.map_openai_params = map_without_temperature


def configure_model(settings, config, provider):
    """Translate the installed profile and shared effort to upstream settings."""
    profile = config["providers"][provider]
    model = profile["model"]
    reasoning = profile["reasoning"]
    settings.set("config.model", model)
    settings.set("config.ai_timeout", model_timeout(config, provider))
    settings.set("config.temperature", config["review"]["temperature"])
    if reasoning != "none":
        settings.set("config.reasoning_effort", config["review"]["effort"])
    if reasoning == "gemini_thinking_level":
        settings.set("config.no_temperature_models", [model])
        settings.set("config.additional_reasoning_effort_models", [model])
        omit_gemini_temperature()
    elif reasoning == "claude_adaptive":
        settings.set("config.enable_claude_adaptive_thinking", True)


# Upstream guidance bullets that discourage reporting policy violations without a bug scenario.
UPSTREAM_GUIDANCE = {
    "finding": "Do not flag intentional design choices or stylistic preferences unless they introduce a clear defect.",
    "certainty": "For lower-severity concerns, be certain before flagging. If you cannot confidently explain why "
                 "something is a problem with a concrete scenario, do not flag it.",
    "uncertainty": "When confidence is limited but the potential impact is high (e.g., data loss, security), report "
                   "it with an explicit note on what remains uncertain. Otherwise, prefer not reporting over guessing.",
    "comments": "Be direct about why something is a problem and the realistic scenario where it manifests.",
}


RULE_CHECK_LABELS = {"violated": "❌ violated", "compliant": "✅ compliant", "not_applicable": "➖ not applicable"}
RULE_CHECK_EVIDENCE_CHARS = 300


def table_cell(value, limit=None):
    """Make untrusted model text safe for one Markdown table cell."""
    text = " ".join(str(value).split()) if value is not None else ""
    if limit is not None and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return html.escape(text).replace("|", "&#124;")


def render_rule_checks(checks):
    """Render the per-rule checklist as one collapsible table for the PR comment."""
    rows, counts, violated = [], {}, []
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        rule = table_cell(check.get("rule_id"))
        status = " ".join(str(check.get("status", "")).split()).lower().replace(" ", "_")
        counts[status] = counts.get(status, 0) + 1
        if status == "violated":
            violated.append(rule)
        label = RULE_CHECK_LABELS.get(status, f"❔ {table_cell(status)}")
        rows.append(f"| {rule} | {label} | {table_cell(check.get('evidence'), RULE_CHECK_EVIDENCE_CHARS)} |")
    if not rows:
        return ""
    summary = [f"❌ {', '.join(violated)} violated"] if violated else []
    summary += [f"{counts[key]} {RULE_CHECK_LABELS[key].split(' ', 1)[1]}"
                for key in ("compliant", "not_applicable") if counts.get(key)]
    return ("\n\n<details><summary><strong>Rule checks</strong>: " + " · ".join(summary or ["no verdicts"])
            + "</summary>\n\n| Rule | Verdict | Evidence |\n|---|---|---|\n" + "\n".join(rows) + "\n\n</details>\n")


def render_review_markdown(render):
    """Wrap PR-Agent's renderer so rule_checks appear as a table, not a raw YAML dump."""
    def render_with_rule_checks(output_data, *args, **kwargs):
        review = output_data.get("review") if isinstance(output_data, dict) else None
        if not isinstance(review, dict) or "rule_checks" not in review:
            return render(output_data, *args, **kwargs)
        # Render a copy without the checklist, so the structured report keeps it unchanged.
        others = {key: value for key, value in review.items() if key != "rule_checks"}
        return render({**output_data, "review": others}, *args, **kwargs) + render_rule_checks(review["rule_checks"])
    return render_with_rule_checks


def replace_once(text, pattern, replacement):
    """Apply one anchored edit, failing closed if the pinned prompt structure changed."""
    text, matches = re.subn(pattern, lambda match: f"{match.group(1)}{replacement}", text, flags=re.M)
    if matches != 1:
        raise SettingsError("The pinned PR-Agent review prompt has an unexpected structure")
    return text


def configure_review_prompt(settings, review, rules, policy):
    """Align the pinned prompt's guidance, schema and example with the approved rules.

    The upstream prompt targets bugs and asks the model to prefer silence when
    unsure. Policy review needs the opposite bias, a per-rule checklist the
    gate can verify, and an example that is consistent with its own finding.
    """
    example_rule = review["example_rule_id"]
    if example_rule not in rules:
        raise SettingsError("The example rule ID must exist in the approved catalog")
    checks = review["rule_checks"]
    system = settings.get("pr_review_prompt.system")
    for key, upstream in UPSTREAM_GUIDANCE.items():
        system = replace_once(system, rf"^(- ){re.escape(upstream)}$", review["guidance"][key])
    for field, description in review["field_descriptions"].items():
        system = replace_once(system, rf"^([ \t]+{re.escape(field)}:[^\n]*?= Field\().*\)$",
                              f"description={json.dumps(description)})")

    rule_check_model = (
        "class RuleCheck(BaseModel):\n"
        f'    rule_id: str = Field(description={json.dumps("One approved engineering rule ID in the form ENG-NNN")})\n'
        '    status: Literal["violated", "compliant", "not_applicable"] = '
        f"Field(description={json.dumps(checks['status'])})\n"
        f"    evidence: str = Field(description={json.dumps(checks['evidence'])})\n\n"
    )
    system = replace_once(system, r"^()(?=class Review\(BaseModel\):$)", rule_check_model)
    system = replace_once(
        system, r"^([ \t]+)(?=key_issues_to_review: List\[KeyIssuesComponentLink\])",
        f"rule_checks: List[RuleCheck] = Field(description={json.dumps(checks['description'])})\n    ",
    )

    # Keep the example internally consistent: a required finding cannot accompany a clean recommendation.
    required = rules[example_rule] == "required"
    system = replace_once(system, r"^(  risk_level: )low$", "high" if required else "medium")
    system = replace_once(system, r"^(  merge_recommendation: )no_concerns_found$",
                          "changes_required" if required else "no_concerns_found")
    system = replace_once(
        system, r"^()(?=  key_issues_to_review:$)",
        f"  rule_checks:\n    - rule_id: |\n        {example_rule}\n      status: |\n        violated\n"
        "      evidence: |\n        ...\n    - ...\n",
    )
    system = replace_once(system, r"^([ \t]+)Possible Bug$", example_rule)
    settings.set("pr_review_prompt.system", system)
    settings.set("config.system_instructions", f"{review['policy_preamble']}\n\n{policy}")
    settings.set("pr_reviewer.extra_instructions", review["additional_instructions"])


def run_review(progress, forge, url, head_sha, base_sha, report_path, snapshot_path, model_error_path, model_provider):
    """Run pinned PR-Agent and write artifacts for the credentialed parent process."""
    config = load_settings()
    model = config["providers"][model_provider]["model"]
    max_files = config["limits"]["changed_files"]

    class PinnedGithubProvider(GithubProvider):
        incomplete = False

        def get_files(self):
            if self.git_files is None:
                try:
                    self._load_pinned_comparison()
                except IncompleteInput as error:
                    PinnedGithubProvider.incomplete = True
                    progress(f"Comparison rejected ({error})")
                    raise
            return self.git_files

        def _load_pinned_comparison(self):
            if self.pr.head.sha != head_sha or self.pr.base.sha != base_sha:
                raise IncompleteInput("PR identity changed")
            if not 0 < self.pr.changed_files <= max_files:
                raise IncompleteInput(f"Changed-file count {self.pr.changed_files} must be between 1 and {max_files}")

            progress("Fetching the pinned GitHub comparison")
            comparison = self.repo_obj.compare(base_sha, head_sha)
            files = list(comparison.files)
            if len(files) != self.pr.changed_files:
                raise IncompleteInput("comparison file list is incomplete")
            changed_lines = sum(file.additions + file.deletions for file in files)
            validate_comparison_size(len(files), changed_lines)
            if any(not complete_patch(file) for file in files):
                raise IncompleteInput("comparison contains an unavailable or incomplete patch")
            progress(f"Validated the GitHub comparison with {len(files)} changed files and {changed_lines} changed lines")
            self.git_files = files
            self.pinned_merge_base = comparison.merge_base_commit

        def _get_merge_base_commit(self):
            self.get_files()
            return self.pinned_merge_base

        def publish_structured_review(self, _report):
            # Record coverage inputs directly from the provider, outside model output.
            diff_files = [file for file in self.get_diff_files() if file.patch and not file.content_fetch_failed]
            snapshot = {
                "head_sha": self.pr.head.sha,
                "base_sha": self.pr.base.sha,
                "changed_files": [file.filename for file in self.get_files()],
                "review_files": [file.filename for file in diff_files],
            }
            if config["review"]["inline_comments"]:
                snapshot["added_lines"] = added_line_numbers(diff_files)
            Path(snapshot_path).write_text(json.dumps(snapshot), encoding="utf-8")

    provider_id = f"demo-pinned-{forge}"
    provider = PinnedGithubProvider
    if forge == "gitea":
        provider = create_gitea_provider(head_sha, base_sha, Path(snapshot_path), progress)
    register_git_provider(provider_id, provider)
    pr_reviewer.convert_to_markdown_v2 = render_review_markdown(pr_reviewer.convert_to_markdown_v2)
    settings = get_settings()
    if version("pr-agent") != PR_AGENT_VERSION:
        raise RuntimeError("unexpected PR-Agent version")
    for section, values in config["pr_agent"].items():
        for key, value in values.items():
            if not settings.exists(f"{section}.{key}"):
                raise RuntimeError(f"unsupported upstream setting {section}.{key}")
            settings.set(f"{section}.{key}", value)

    rules, policy, _ = load_policy(ROOT / "policies")
    settings.set("config.git_provider", provider_id)
    configure_model(settings, config, model_provider)
    settings.set("pr_reviewer.persistent_comment", config["forges"][forge]["persistent_comment"])
    try:
        configure_review_prompt(settings, config["review"], rules, policy)
    except SettingsError as error:
        progress(str(error))
        return 1
    sys.argv = ["pr-agent", "--pr_url", url, "--json-output", report_path, "review"]

    observed_error = None

    def remember_failure(message):
        nonlocal observed_error
        exception = message.record["exception"]
        if exception is not None:
            observed_error = exception.value

    # The pinned CLI logs the original exception here before returning 1.
    # Observe the exception object rather than copying or parsing its output.
    observer = logger.add(
        remember_failure,
        level="ERROR",
        filter=lambda record: record["name"] == "pr_agent.agent.pr_agent" and record["function"] == "_handle_request",
        format=lambda _: "{level}",
    )
    request_timeout = settings.get("config.ai_timeout")
    progress_messages = {
        (*COMPLETION_LOG_SOURCE, "Prompts"):
            f"Submitting the review to {model} with a {request_timeout}s model request limit",
        ("pr_agent.tools.pr_reviewer", "run", "PR output"):
            "Prepared the structured review and rendered the PR comment",
    }

    log_output = review_logging_enabled()
    secrets = (
        os.environ.get("GITEA__PERSONAL_ACCESS_TOKEN" if forge == "gitea" else "GITHUB__USER_TOKEN"),
        os.environ.get(PR_AGENT_KEY_VARIABLES[model_provider]),
    )

    # Forward fixed stage markers and, when enabled, just the response text.
    def progress_message(record):
        return progress_messages.get((record["name"], record["function"], record["message"]))

    def is_model_output(record):
        return (log_output and record["level"].name == "DEBUG"
                and (record["name"], record["function"]) == COMPLETION_LOG_SOURCE
                and record["message"].startswith(MODEL_OUTPUT_PREFIX))

    def show_progress(message):
        record = message.record
        if stage := progress_message(record):
            progress(stage)
        else:
            response = record["message"].removeprefix(MODEL_OUTPUT_PREFIX)
            log_review_data(progress, "Model output", response, secrets)

    progress_observer = logger.add(
        show_progress,
        level="DEBUG",
        filter=lambda record: progress_message(record) is not None or is_model_output(record),
        format=lambda _: "{level}",
    )
    try:
        result = run()
    except Exception as error:
        if classify_failure(observed_error) is None:
            observed_error = error
        result = 1
    finally:
        logger.remove(progress_observer)
        logger.remove(observer)
    if provider.incomplete:
        return ReviewExit.INCOMPLETE_PR_INPUT
    if result:
        if details := model_failure_details(observed_error, model_provider):
            Path(model_error_path).write_text(json.dumps(details), encoding="utf-8")
        return classify_failure(observed_error) or result
    return result


if __name__ == "__main__":
    with os.fdopen(int(sys.argv[1]), "w", encoding="utf-8") as stream:
        raise SystemExit(run_review(partial(log_progress, stream=stream), *sys.argv[2:]))
