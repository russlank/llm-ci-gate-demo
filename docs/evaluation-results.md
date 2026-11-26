# Evaluation results

Local evidence for this revision and reviewer image 0.4.5, from November 26
and 27, 2026. These checks cover conventional CI and the gate's deterministic
decisions with fixtures. Live model accuracy is measured separately with the
[demonstration procedure](demonstration-guide.md).

| Check | Result |
|---|---|
| `./ci/dotnet-gates.sh` in `mcr.microsoft.com/dotnet/sdk:10.0` | Locked restore, warning-free Release build, 11 xUnit cases and format verification passed |
| Scenario patches | All 16 patches apply to the `scenarios-v1` tag. The conventional gate passed for 15 scenarios and failed only SC10's intended assertion |
| `PYTHONPATH=ci/gate python3 -m unittest discover -s ci/gate/tests` | 38 cases passed: required, advisory, incomplete and rule-check outcomes, inline comments, provider failures and stale heads |
| Reviewer image tests | 16 cases passed in the 0.4.5 image, including Gitea comparison limits, the rendered review prompt, and the rule-check table in the PR comment |
| Review prompt | The request captured for a PR contained every changed file, the full rule catalog, the per-rule checklist schema, and an example consistent with its required finding |
| Workflows | actionlint with shellcheck reported no errors for the GitHub Actions workflows. `ci/select-pipeline.sh check` passed |

With stubbed model replies for a PR containing ENG-001, ENG-004 and ENG-009
violations, a reply that lists them returns `FAIL` with all three IDs. A reply
that omits the per-rule checklist returns `REVIEW_REQUIRED` instead of
passing. Whether a live model reports the violations is not covered by these
checks.
