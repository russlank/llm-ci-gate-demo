"""Orchestrate a pinned semantic review and emit one machine-readable decision.

The parent verifies forge identity before and after the child runs. Progress
and optional review details go to stderr, and the decision goes to stdout.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from evaluate import (
    GEMINI_ERROR_STATUSES, MODEL_TIMEOUT_PHASES, PR_AGENT_VERSION, SHA, Decision, ReviewExit, evaluate, load_policy,
    normalized_finding,
)
from settings import MODEL_KEY_VARIABLES, PR_AGENT_KEY_VARIABLES, SettingsError, load_settings

ROOT = Path(__file__).resolve().parents[2]
REPO_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
PR_NUMBER = r"[1-9][0-9]*"
GITHUB_URL = "https://github.com"
TOKEN_VARIABLES = {"github": "PLUGIN_GITHUB_TOKEN", "gitea": "PLUGIN_GITEA_TOKEN"}
REVIEW_LOG_VARIABLE = "PLUGIN_LOG_REVIEW_OUTPUT"


def log_progress(message, stream=None):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[semantic-review] {timestamp} {message}", file=stream or sys.stderr, flush=True)


def review_logging_enabled():
    override = os.environ.get(REVIEW_LOG_VARIABLE)
    return load_settings()["review"]["log_output"] if override is None else override.lower() == "true"


def log_review_data(progress, label, data, secrets):
    """Emit bounded review text with escaped controls and configured secrets redacted."""
    payload = json.dumps(data, ensure_ascii=True)
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        payload = payload.replace(json.dumps(secret, ensure_ascii=True)[1:-1], "[REDACTED]")
    max_chars = load_settings()["limits"]["review_log_chars"]
    if len(payload) > max_chars:
        payload = payload[:max_chars] + " [truncated]"
    progress(f"{label} {payload}")


@dataclass(frozen=True)
class PullTarget:
    """Validated forge routing and the expected head of a Woodpecker or manual run."""

    forge: str
    forge_url: str
    repo: str
    number: str
    expected_head: str | None

    @property
    def pr_url(self):
        route = "pulls" if self.forge == "gitea" else "pull"
        return f"{self.forge_url}/{self.repo}/{route}/{self.number}"


class NoRedirect(HTTPRedirectHandler):
    """Keep authenticated PR lookups on the selected API endpoint."""

    def redirect_request(self, *args, **kwargs):
        return None


def pull_request(target, token):
    """Fetch bounded PR metadata using the selected forge token."""
    return forge_json(target, token)


def forge_json(target, token, suffix="", payload=None):
    """Use the same bounded, authenticated transport for PR metadata and reviews."""
    gitea = target.forge == "gitea"
    api = f"{target.forge_url}/api/v1" if gitea else "https://api.github.com"
    request = Request(
        f"{api}/repos/{target.repo}/pulls/{target.number}{suffix}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Accept": "application/json" if gitea else "application/vnd.github+json",
            "Authorization": f"{'token' if gitea else 'Bearer'} {token}",
            "Content-Type": "application/json",
            "User-Agent": "llm-ci-gate-demo",
        },
    )
    max_bytes = load_settings()["limits"]["json_bytes"]
    timeout = load_settings()["timeout_seconds"]["forge"]
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("PR response exceeds size limit")
    return json.loads(data)


def publish_inline_findings(target, token, key, before, report, snapshot, rules, settings):
    """Publish one comment-only review for eligible findings on the pinned head."""
    head = before["head_sha"]
    limits = settings["limits"]
    findings = report["review"]["key_issues_to_review"]
    max_findings = settings["pr_agent"]["pr_reviewer"]["num_max_findings"]
    anchors = snapshot.get("added_lines", {})
    comments = {}
    for finding in findings:
        finding = normalized_finding(finding, snapshot["review_files"], rules)
        if finding is None:
            continue
        path = finding["relevant_file"]
        line = next((line for line in anchors.get(path, [])
                     if finding["start_line"] <= line <= finding["end_line"]), None)
        if line is None:
            continue
        identifier = finding["issue_header"]
        digest = hashlib.sha256(json.dumps([head, path, line, identifier]).encode()).hexdigest()
        marker = f"<!-- llm-ci-gate-inline:{digest} -->"
        body = finding["issue_content"].strip()
        for secret in sorted((token, key), key=len, reverse=True):
            body = body.replace(secret, "[REDACTED]")
        body = re.sub(r"(`{3,}[ \t]*)suggestion\b", r"\1text", body, flags=re.IGNORECASE)
        anchor = {"new_position": line, "old_position": 0} if target.forge == "gitea" else {"line": line, "side": "RIGHT"}
        comments.setdefault(marker, {
            "path": path, "body": f"**{identifier}**\n\n{body[:limits['inline_comment_chars']]}", **anchor,
        })
        if len(comments) >= max_findings:
            break
    if not comments:
        log_progress("No review findings could be anchored to added lines")
        return 0

    # Stable per-head markers in the review body suppress duplicates on a retry.
    size_key = "limit" if target.forge == "gitea" else "per_page"
    for page in range(1, limits["inline_review_pages"] + 1):
        reviews = forge_json(target, token, f"/reviews?{size_key}=50&page={page}")
        if not isinstance(reviews, list):
            raise ValueError("Expected a list of reviews")
        for review in reviews:
            if review.get("commit_id") != head or not isinstance(review.get("body"), str):
                continue
            for marker in tuple(comments):
                if marker in review["body"]:
                    del comments[marker]
        if not comments or len(reviews) < 50:
            break
    else:
        log_progress("Inline comments skipped because review history exceeded the configured limit")
        return 0

    if comments:
        forge_json(target, token, "/reviews", {
            "commit_id": head,
            "event": "COMMENT",
            "body": f"Semantic review findings for `{head}`\n\n" + "\n".join(comments),
            "comments": list(comments.values()),
        })
    log_progress(f"Published {len(comments)} inline finding comments for the pinned head")
    return len(comments)


def gitea_url(value):
    """Validate a trusted Gitea base URL, including an optional install path."""
    url = urlsplit(value)
    path = url.path.rstrip("/")
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", url.netloc)
            or (path and not re.fullmatch(r"(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)+", path))):
        raise ValueError("invalid Gitea URL")
    return f"{url.scheme}://{url.netloc}{path}"


def pr_target(args, forge):
    """Validate either a manual PR URL or Woodpecker's PR event metadata."""
    base = GITHUB_URL
    if forge == "gitea":
        base = gitea_url(args.gitea_url if args.pr_url is not None else os.environ.get("CI_FORGE_URL", ""))
    if args.pr_url is not None:
        route = "pulls" if forge == "gitea" else "pull"
        match = re.fullmatch(rf"{re.escape(base)}/({REPO_PATTERN})/{route}/({PR_NUMBER})", args.pr_url)
        if match is None:
            raise ValueError("invalid PR URL for the configured forge")
        repo, number = match.groups()
        return PullTarget(forge, base, repo, number, args.head)
    if args.head is not None or args.gitea_url is not None:
        raise ValueError("--head and --gitea-url require --pr-url")

    repo = os.environ.get("CI_REPO", "")
    number = os.environ.get("CI_COMMIT_PULL_REQUEST", "")
    head = os.environ.get("CI_COMMIT_SHA", "")
    if (os.environ.get("CI_PIPELINE_EVENT") != "pull_request"
            or os.environ.get("CI_COMMIT_TARGET_BRANCH") != load_settings()["target_branch"]
            or not re.fullmatch(REPO_PATTERN, repo)
            or not re.fullmatch(PR_NUMBER, number)
            or not SHA.fullmatch(head)):
        raise ValueError("invalid Woodpecker pull request metadata")
    return PullTarget(forge, base, repo, number, head)


def identity(pr, repo, number):
    """Return the SHAs of an eligible open, same-repository pull request."""
    head = pr["head"]["sha"]
    base = pr["base"]["sha"]
    if (pr["state"] != "open" or pr["number"] != int(number)
            or pr["base"]["repo"]["full_name"].lower() != repo.lower()
            or pr["head"]["repo"]["full_name"].lower() != repo.lower()
            or pr["base"]["ref"] != load_settings()["target_branch"]
            or not SHA.fullmatch(head) or not SHA.fullmatch(base)):
        raise ValueError("PR must be open, same-repository and targeting the configured branch")
    return {"head_sha": head, "base_sha": base}


def read_report(path):
    """Decode a child artifact within the configured JSON size limit."""
    max_bytes = load_settings()["limits"]["json_bytes"]
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("report exceeds size limit")
    return json.loads(data)


def read_model_error(path, failure):
    """Take only the allowed status and timeout fields from the child artifact."""
    try:
        artifact = read_report(path)
    except (OSError, ValueError):
        return {}
    if not isinstance(artifact, dict):
        return {}
    if failure == ReviewExit.LLM_TIMED_OUT:
        phase = artifact.get("model_timeout_phase")
        if isinstance(phase, str) and phase in MODEL_TIMEOUT_PHASES:
            return {"model_timeout_phase": phase}
    status = artifact.get("model_http_status")
    if type(status) is not int or not 400 <= status <= 599:
        return {}
    details = {"model_http_status": status}
    error_status = artifact.get("model_error_status")
    if isinstance(error_status, str) and error_status in GEMINI_ERROR_STATUSES:
        details["model_error_status"] = error_status
    return details


def run_pr_agent(target, snapshot, work, token, provider, key):
    """Launch the reviewer with one provider key and read its report or typed failure."""
    report_path = work / "review.json"
    snapshot_path = work / "snapshot.json"
    model_error_path = work / "model-error.json"
    credential_env = {"GITHUB__USER_TOKEN": token}
    if target.forge == "gitea":
        credential_env = {"GITEA__PERSONAL_ACCESS_TOKEN": token, "GITEA__URL": target.forge_url}
    # Give the reviewer a dedicated descriptor for progress. The SDK's
    # unrestricted stdout and stderr still go to /dev/null.
    progress_fd = os.dup(2)
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "ci" / "pr-agent" / "review.py"), str(progress_fd), target.forge,
             target.pr_url, snapshot["head_sha"], snapshot["base_sha"], str(report_path), str(snapshot_path),
             str(model_error_path), provider],
            cwd=work,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(work),
                "PYTHONPATH": "/app",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                REVIEW_LOG_VARIABLE: "true" if review_logging_enabled() else "false",
                **credential_env,
                PR_AGENT_KEY_VARIABLES[provider]: key,
            },
            pass_fds=(progress_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=load_settings()["timeout_seconds"]["reviewer"],
            check=False,
        )
    finally:
        os.close(progress_fd)
    if result.returncode:
        try:
            failure = ReviewExit(result.returncode)
        except ValueError:
            return Decision("ERROR", "pr_agent_failed"), {}
        outcome = "REVIEW_REQUIRED" if failure == ReviewExit.INCOMPLETE_PR_INPUT else "ERROR"
        reason = failure.name.lower()
        if reason.startswith("llm_"):
            decision = Decision(outcome, f"{provider}_{reason.removeprefix('llm_')}")
            return decision, read_model_error(model_error_path, failure)
        return Decision(outcome, reason), {}
    if not report_path.is_file() or not snapshot_path.is_file():
        return Decision("ERROR", "missing_pr_agent_artifact"), {}
    return read_report(report_path), read_report(snapshot_path)


def review_pr(args, details):
    """Review the expected PR head and evaluate the report while that identity is current."""
    settings = load_settings()
    if args.pr_url is not None:
        forge = "gitea" if args.gitea_url is not None else "github"
    else:
        forge = os.environ.get("CI_FORGE_TYPE", "")
    if forge not in TOKEN_VARIABLES:
        return Decision("ERROR", "unsupported_forge")
    details["forge"] = forge

    try:
        target = pr_target(args, forge)
    except ValueError:
        return Decision("ERROR", "invalid_pr_metadata")
    provider = os.environ.get("PLUGIN_LLM_PROVIDER", settings["default_provider"])
    if provider not in MODEL_KEY_VARIABLES:
        return Decision("ERROR", "unsupported_llm_provider")
    details["llm_provider"] = provider
    token = os.environ.get(TOKEN_VARIABLES[forge])
    key = os.environ.get(MODEL_KEY_VARIABLES[provider])
    if not token or not key:
        return Decision("ERROR", "missing_credentials")

    rules, _, digest = load_policy(ROOT / "policies")
    model = settings["providers"][provider]["model"]
    details.update(repo=target.repo, pr_number=int(target.number), policy_sha256=digest, model=model)
    log_progress(f"Checking {forge} PR {target.repo}#{target.number} with {provider} model {model}")
    try:
        before = identity(pull_request(target, token), target.repo, target.number)
    except (AttributeError, KeyError, TypeError, ValueError):
        return Decision("REVIEW_REQUIRED", "invalid_pr_identity")
    details.update(before)
    if target.expected_head is not None and before["head_sha"] != target.expected_head:
        return Decision("REVIEW_REQUIRED", "pipeline_head_is_stale")

    log_progress(f"Pinned PR head {before['head_sha']} and base {before['base_sha']}")
    reviewer_timeout = settings["timeout_seconds"]["reviewer"]
    log_progress(f"Starting PR-Agent {PR_AGENT_VERSION} with a {reviewer_timeout}s process limit")
    with tempfile.TemporaryDirectory(prefix="llm-ci-gate-") as work:
        try:
            report, artifact = run_pr_agent(target, before, Path(work), token, provider, key)
        except subprocess.TimeoutExpired:
            return Decision("ERROR", "pr_agent_timeout")
        except (UnicodeError, ValueError):
            return Decision("ERROR", "invalid_pr_agent_artifact")

    log_progress("Rechecking PR head and base after the reviewer finished")
    # A force-push or base update during review invalidates its decision for this run.
    try:
        after = identity(pull_request(target, token), target.repo, target.number)
    except (AttributeError, KeyError, TypeError, ValueError):
        return Decision("REVIEW_REQUIRED", "pr_identity_changed")
    if after != before:
        return Decision("REVIEW_REQUIRED", "pr_identity_changed")
    if isinstance(report, Decision):
        details.update(artifact)
        return report

    if review_logging_enabled():
        log_review_data(log_progress, "Structured review report", report, (token, key))

    log_progress(f"Evaluating review coverage and {len(rules)} engineering rules")
    usage = report.get("usage") if isinstance(report, dict) else None
    if isinstance(usage, dict):
        details["usage"] = {
            field: usage[field] for field in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(usage.get(field)) is int and usage[field] >= 0
        }
    decision = evaluate(report, artifact, before, rules, settings["pr_agent"]["pr_reviewer"]["num_max_findings"])
    if settings["review"]["inline_comments"] and decision.outcome in {"FAIL", "WARNING"}:
        try:
            details["inline_comments_posted"] = publish_inline_findings(
                target, token, key, before, report, artifact, rules, settings,
            )
        except HTTPError as error:
            details["inline_comment_http_status"] = error.code
            error.close()
            log_progress(f"Inline comment publication failed (HTTP {error.code})")
        except (OSError, ValueError, TypeError, AttributeError):
            log_progress("Inline comment publication failed")
    return decision


def main():
    """Translate input and runtime failures into the public JSON and exit contract."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-url", help="review a PR locally without Woodpecker")
    parser.add_argument("--gitea-url", help="trusted Gitea instance URL for a local PR review")
    parser.add_argument("--head", type=lambda value: value if SHA.fullmatch(value) else parser.error("invalid head SHA"))
    args = parser.parse_args()
    started = time.monotonic()
    details = {"pr_agent_version": PR_AGENT_VERSION}
    try:
        decision = review_pr(args, details)
    except SettingsError as error:
        log_progress(str(error))
        decision = Decision("ERROR", "invalid_gate_configuration")
    except HTTPError as error:
        details["forge_http_status"] = error.code
        error.close()
        decision = Decision("ERROR", "forge_pr_lookup_http_error")
    except (URLError, TimeoutError):
        decision = Decision("ERROR", "forge_pr_lookup_network_error")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        decision = Decision("ERROR", "gate_infrastructure_failure")

    if decision.invalid_fields:
        details["invalid_review_fields"] = decision.invalid_fields
        log_progress(f"Invalid review fields {', '.join(decision.invalid_fields)}")
    log_progress(f"Finished with {decision.outcome} ({decision.reason})")
    print(json.dumps({
        **details,
        "decision": decision.outcome,
        "reason": decision.reason,
        "required_rule_ids": decision.required,
        "advisory_rule_ids": decision.advisory,
        "duration_seconds": round(time.monotonic() - started, 2),
    }, sort_keys=True))
    return decision.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
