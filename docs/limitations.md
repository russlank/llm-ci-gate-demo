# Limitations

- The reviewer sees diff hunks and selected context. An LLM can miss a
  violation, report an allowed change, or follow misleading PR content.
  Structured output and coverage checks help validate a report. Policy
  accuracy still needs measured examples and human review.
- A missing patch, more than 50 changed files, more than 2,000 added and
  deleted lines, upstream filtered files, or model truncation blocks the
  gate. This intentionally limits the demo to small textual changes. PR-Agent
  v0.50.0 filters some generated files, including NuGet lockfiles, so changes
  to those files need a separate review path before this gate can cover them.
- A same-repository PR targeting `main` is required. The semantic status is
  SHA-scoped. The operator must verify status publication and configure the
  ruleset's freshness requirements, especially when the target advances.
- The Gitea adapter reviews the Git patch with standard hunk context. It
  blocks binary or metadata-only changes, unsupported paths, and comparisons
  with ambiguous merge bases. PR-Agent posts a new SHA-labeled comment for
  each Gitea review.
- When API calls or report validation fail, inspect the sanitized reason and
  rerun from the trusted workflow. `forge_pr_lookup_http_error` includes the
  HTTP status in `forge_http_status`. On `404`, verify the repository and PR
  number on the configured forge and confirm the reviewer token can read it.
  On `forge_pr_lookup_network_error`,
  check connectivity from the runner to the configured forge. The credentialed
  job discards raw process output. The detailed review log option can include
  excerpts from the PR in the model response and structured report.
  [GitHub REST API troubleshooting](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api)
- Reviewer failures now report a fixed reason where the cause is known.
  `gitea_git_fetch_failed` means the runner could not fetch the pinned commits
  over Git HTTP. `gitea_comment_failed` points to comment publication. Model
  failures use the selected provider prefix, such as `openai_rate_limited`
  or `gemini_rate_limited`. The original exception text stays out of the CI log.
- Gemini reviews request low thinking and allow 150 seconds per model call.
  The gate caps the entire reviewer process at 240 seconds. A
  `gemini_timed_out` result blocks merging pending a complete review.
  [Gemini thinking levels](https://ai.google.dev/gemini-api/docs/thinking)
- For production, conventional checks are better suited to named flag
  identifiers (Roslyn), disabled configuration defaults (schema assertions),
  tests for known flags (xUnit), and empty catch blocks (Roslyn). Review
  whether a change introduces behavior, which policies apply, and whether
  compatibility is affected using the semantic or human path.

## Native PR-Agent status comparison on GitHub

The pinned release implements the model's `merge_recommendation` field and
has an option to publish the review itself as a check run. Its release
configuration and reviewer source lack
`pr_reviewer.publish_merge_recommendation_status`. The strict workflow
therefore leaves both native publishing and automatic approval disabled.
There is no observed native status result for the pinned release.
[Pinned v0.50.0 configuration](https://github.com/The-PR-Agent/pr-agent/blob/v0.50.0/pr_agent/settings/configuration.toml) · [Pinned reviewer](https://github.com/The-PR-Agent/pr-agent/blob/v0.50.0/pr_agent/tools/pr_reviewer.py)

When support is released, pin and validate that release separately. Use an
authorized GitHub App with appropriate Checks write permission and enable
`require_merge_recommendation` and `publish_merge_recommendation_status` for
that comparison run. Keep the strict workflow independently configured. Start
the comparison after the conventional status is published and keep it outside
the dependency chain of statuses it consumes.

| Case | Documented native result to verify on the supported release |
|---|---|
| Successful evaluation with all deterministic checks passing | `no_concerns_found`, success |
| Model identifies a clear blocker | `changes_required`, failure |
| Inconclusive result or missing CI | `needs_review`, neutral |
| Stale base, sensitive files, or changed-line limit exceeded | Deterministic veto of a clean recommendation, at least `needs_review` |

GitHub accepts neutral and skipped required-check conclusions. For the demo,
keep the strict Woodpecker status required so uncertainty remains blocking.
[PR-Agent native status documentation](https://docs.pr-agent.ai/commands/review/#merge-recommendation) · [GitHub required status behavior](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)
