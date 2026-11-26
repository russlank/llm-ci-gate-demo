"""Evaluate the pinned PR-Agent review contract using the approved rule catalog."""

import hashlib
import json
import re
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

PR_AGENT_VERSION = "0.50.0"
RULE_ID = re.compile(r"ENG-[0-9]{3}")
SHA = re.compile(r"[0-9a-f]{40}")
EXIT_CODES = {"PASS": 0, "WARNING": 0, "FAIL": 1, "REVIEW_REQUIRED": 2, "ERROR": 3}
GEMINI_ERROR_STATUSES = frozenset({
    "DEADLINE_EXCEEDED", "FAILED_PRECONDITION", "INTERNAL", "INVALID_ARGUMENT",
    "NOT_FOUND", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "UNAUTHENTICATED", "UNAVAILABLE",
})
MODEL_TIMEOUT_PHASES = frozenset({"connect", "read", "write", "pool"})
RULE_CHECK_STATUSES = frozenset({"violated", "compliant", "not_applicable"})


class ReviewExit(IntEnum):
    """Internal reviewer exits that the parent translates into gate decisions."""

    INCOMPLETE_PR_INPUT = 20
    GITEA_GIT_FETCH_FAILED = 21
    GITEA_API_ERROR = 22
    GITEA_COMMENT_FAILED = 23
    LLM_AUTHENTICATION_FAILED = 24
    LLM_ACCESS_DENIED = 25
    LLM_MODEL_UNAVAILABLE = 26
    LLM_QUOTA_EXHAUSTED = 27
    LLM_RATE_LIMITED = 28
    LLM_CONNECTION_FAILED = 29
    LLM_REQUEST_FAILED = 30
    PR_AGENT_RESPONSE_INVALID = 31
    LLM_TIMED_OUT = 32


@dataclass(frozen=True)
class Decision:
    """Gate outcome and the fixed metadata reported to CI."""

    outcome: str
    reason: str
    required: tuple[str, ...] = ()
    advisory: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()

    @property
    def exit_code(self):
        return EXIT_CODES[self.outcome]


def load_policy(root: Path):
    """Pair catalog entries with Markdown rules and fingerprint the exact bytes."""
    catalog = (root / "rule-catalog.yaml").read_bytes()
    markdown = (root / "engineering-rules.md").read_bytes()
    parsed = json.loads(catalog)
    if parsed["version"] != 1:
        raise ValueError("unsupported policy schema")

    sections = re.split(r"(?m)^## (ENG-[0-9]{3})\s*$", markdown.decode())
    descriptions = dict(zip(sections[1::2], sections[2::2], strict=True))
    rules = {}
    instructions = []
    for rule in parsed["rules"]:
        identifier = rule["id"]
        if (not RULE_ID.fullmatch(identifier) or identifier in rules
                or not descriptions.get(identifier, "").strip()
                or rule["enforcement"] not in {"required", "advisory"}
                or rule["severity"] not in {"high", "medium", "low"}):
            raise ValueError("invalid policy rule")
        rules[identifier] = rule["enforcement"]
        instructions.append(
            f"{identifier} — {rule['name']} ({rule['enforcement']}, {rule['severity']})\n"
            f"{descriptions[identifier].strip()}"
        )

    if not rules or len(descriptions) != len(sections[1::2]) or rules.keys() != descriptions.keys():
        raise ValueError("policy catalog and descriptions disagree")

    digest = hashlib.sha256(catalog + b"\0" + markdown).hexdigest()
    return rules, "\n\n".join(instructions), digest


def text_field(value):
    return isinstance(value, str) and bool(value.strip())


def review_enum(value):
    return value.strip().lower().replace(" ", "_") if isinstance(value, str) else None


def normalized_finding(finding, reviewed_paths, rules):
    """Return a finding with trimmed identifiers when it is classifiable, otherwise None.

    The prompt asks for YAML block scalars, which keep a trailing newline, so
    the rule ID and path are compared after trimming surrounding whitespace.
    """
    if not isinstance(finding, dict):
        return None
    identifier = finding.get("issue_header")
    path = finding.get("relevant_file")
    start = finding.get("start_line")
    end = finding.get("end_line")
    if not isinstance(identifier, str) or not isinstance(path, str):
        return None
    identifier, path = identifier.strip(), path.strip()
    if (identifier not in rules or path not in reviewed_paths
            or not text_field(finding.get("issue_content"))
            or type(start) is not int or type(end) is not int or not 1 <= start <= end):
        return None
    return {**finding, "issue_header": identifier, "relevant_file": path}


def rule_check_statuses(checks, rules):
    """Map every catalog rule to its reported status, or return None for an incomplete checklist."""
    if not isinstance(checks, list):
        return None
    statuses = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("rule_id"), str):
            return None
        identifier = check["rule_id"].strip()
        status = review_enum(check.get("status"))
        if (identifier not in rules or identifier in statuses or status not in RULE_CHECK_STATUSES
                or not text_field(check.get("evidence"))):
            return None
        statuses[identifier] = status
    return statuses if statuses.keys() == rules.keys() else None


def evaluate(report, snapshot, expected, rules, max_findings):
    """Classify a structured review using the snapshot from the trusted forge adapter.

    Verify commit identity and coverage before interpreting findings. Rule
    enforcement comes from the installed catalog.
    """
    if not isinstance(report, dict) or not isinstance(snapshot, dict):
        return Decision("ERROR", "missing_report_or_snapshot")

    for key in ("head_sha", "base_sha"):
        value = snapshot.get(key)
        if not isinstance(value, str) or not SHA.fullmatch(value) or value != expected.get(key):
            return Decision("REVIEW_REQUIRED", "review_commit_mismatch")

    coverage = report.get("coverage")
    if not isinstance(coverage, dict):
        return Decision("REVIEW_REQUIRED", "missing_coverage")
    for key in ("files_not_reviewed", "files_partially_reviewed"):
        paths = coverage.get(key)
        if not isinstance(paths, list) or not all(text_field(path) for path in paths):
            return Decision("REVIEW_REQUIRED", "invalid_coverage")
        if paths:
            return Decision("REVIEW_REQUIRED", "incomplete_coverage")

    expected_paths = snapshot.get("changed_files")
    reviewed_paths = snapshot.get("review_files")
    if (not isinstance(expected_paths, list) or not expected_paths
            or not isinstance(reviewed_paths, list)
            or not all(text_field(path) for path in expected_paths + reviewed_paths)
            or sorted(expected_paths) != sorted(reviewed_paths)
            or len(expected_paths) != len(set(expected_paths))):
        return Decision("REVIEW_REQUIRED", "incomplete_file_set")

    review = report.get("review")
    if not isinstance(review, dict):
        return Decision("REVIEW_REQUIRED", "missing_review")
    recommendation = review_enum(review.get("merge_recommendation"))
    relevant_tests = review.get("relevant_tests")
    security_concerns = review.get("security_concerns")
    # Unquoted Yes/No in the model's YAML loads as a boolean.
    valid_fields = {
        "merge_recommendation": recommendation in ("no_concerns_found", "needs_review", "changes_required"),
        "risk_level": review_enum(review.get("risk_level")) in ("low", "medium", "high"),
        "relevant_tests": type(relevant_tests) is bool or review_enum(relevant_tests) in ("yes", "no"),
        "security_concerns": security_concerns is False or text_field(security_concerns),
    }
    invalid_fields = tuple(field for field, valid in valid_fields.items() if not valid)
    if invalid_fields:
        return Decision("REVIEW_REQUIRED", "incomplete_review_fields", invalid_fields=invalid_fields)

    findings = review.get("key_issues_to_review")
    if not isinstance(findings, list):
        return Decision("REVIEW_REQUIRED", "missing_findings", invalid_fields=("key_issues_to_review",))

    required, advisory = set(), set()
    ambiguous = False
    for finding in findings:
        finding = normalized_finding(finding, reviewed_paths, rules)
        if finding is None:
            ambiguous = True
            continue
        identifier = finding["issue_header"]
        (required if rules[identifier] == "required" else advisory).add(identifier)

    required_ids = tuple(sorted(required))
    advisory_ids = tuple(sorted(advisory))
    # Once the review contract passes, a known required finding takes priority.
    if required_ids:
        return Decision("FAIL", "required_findings", required_ids, advisory_ids)

    # Without a required finding, the per-rule checklist must show that every
    # approved rule was considered. An empty finding list alone is not evidence.
    statuses = rule_check_statuses(review.get("rule_checks"), rules)
    if statuses is None:
        return Decision("REVIEW_REQUIRED", "incomplete_rule_checks", advisory=advisory_ids,
                        invalid_fields=("rule_checks",))
    # A rule marked violated without an anchored finding cannot be verified or published inline.
    if any(status == "violated" and rule not in advisory for rule, status in statuses.items()):
        return Decision("REVIEW_REQUIRED", "unreported_rule_violations", advisory=advisory_ids,
                        invalid_fields=("rule_checks",))
    if ambiguous or len(findings) >= max_findings:
        return Decision("REVIEW_REQUIRED", "ambiguous_or_capped_findings", advisory=advisory_ids)
    no_security_concerns = security_concerns is False or review_enum(security_concerns) == "no"
    if recommendation != "no_concerns_found" or not no_security_concerns:
        return Decision("REVIEW_REQUIRED", "reviewer_escalated", advisory=advisory_ids)
    if advisory_ids:
        return Decision("WARNING", "advisory_findings", advisory=advisory_ids)
    return Decision("PASS", "complete_review")
