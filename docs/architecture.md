# Architecture and configuration

## HelloWorld

`Program.cs` loads local configuration and wires dependency injection. The
greeting service evaluates the named flags before it applies the corresponding
behavior. Per-feature tests use Microsoft.FeatureManagement and a fixed clock.

The app reads `src/HelloWorld/appsettings.json` from the build or publish
directory. Environment overrides start with `HELLOWORLD_` and use double
underscores for nested keys.

| Flag under `FeatureManagement` | Options under `Greeting` | Effect |
|---|---|---|
| `PersonalizedGreeting` | `Name` | Uses a non-blank name in the greeting |
| `UppercaseGreeting` | — | Uppercases the greeting |
| `TimestampedGreeting` | — | Adds an ISO 8601 UTC prefix |
| `MultilingualGreeting` | `Language` | Uses Spanish (`es`), Swedish (`sv`), or English |
| `StyledGreeting` | `Style` | Wraps the greeting in brackets for `Formal` |

The app prints the greeting to stdout. It stores no name data. Language and
name select the greeting, styling and casing transform it, and the timestamp
is prefixed last. Each flag defaults to `false`.

```sh
HELLOWORLD_FeatureManagement__PersonalizedGreeting=true \
HELLOWORLD_Greeting__Name=Ada dotnet run --project src/HelloWorld
```

The `FeatureManagement` configuration section is supported by the pinned
Microsoft.FeatureManagement version. [Microsoft configuration documentation](https://github.com/microsoft/FeatureManagement-Dotnet/tree/4.8.0)

## Conventional gate

Woodpecker or GitHub Actions executes `ci/dotnet-gates.sh` using the official
.NET 10 SDK image. `ci/pipeline-engine` selects which engine has workflows
installed, and the build first verifies that selection with
`ci/select-pipeline.sh check`.
The script restores locked packages, builds Release with warnings as errors,
runs xUnit, and verifies formatting. The build has no reviewer credentials.

## Semantic gate

PR-Agent v0.50.0 runs in a pinned image. Woodpecker supplies the forge,
repository, pull request number, and expected head SHA. The GitHub Actions
workflow passes the PR URL and expected head SHA instead. The runner checks the
head and base using that forge's PR API. On GitHub, PR-Agent receives the
comparison between the two commit SHAs from GitHub. On Gitea, the adapter
fetches the two commits into a temporary bare Git repository, finds their
merge base, and verifies the textual diff against Git's changed-file and
line-count inventory. The adapter gives PR-Agent the resulting patches
without checking out the PR. The runner checks the target and both SHAs again
after review.

The catalog is YAML in JSON syntax so the adapter can parse it with Python's
standard library. `policies/engineering-rules.md` supplies the rules and
exceptions. The image validates the catalog against the Markdown and generates
the system instructions at runtime. The policy fingerprint covers both files.

PR-Agent writes a human-readable PR comment and a temporary report. Each Gitea comment includes the reviewed head SHA.
The report contains `review.rule_checks`, `review.key_issues_to_review`,
`coverage` and `usage`. The prompt asks the model to record one rule check
per catalog rule (`violated`, `compliant` or `not_applicable`, with evidence)
before it lists findings, and to put one exact policy ID in each
`issue_header`. The image replaces the upstream guidance that tells the model
to prefer silence over uncertain or lower-severity findings.
The installed `ci/gate/settings.yaml` supplies operational defaults and
provider profiles. The image translates the common effort setting to the
selected model's reasoning API.
The adapter validates the coverage, finding shape, path, line range, policy
ID, and review fields. Rule IDs and paths are trimmed because YAML block
scalars keep a trailing newline. It uses catalog enforcement, never model
severity, to classify findings. A classifiable required finding fails the
gate. Otherwise the review must contain a complete rule checklist: a missing,
partial, or duplicated checklist returns `incomplete_rule_checks`, and a rule
marked `violated` without a matching finding returns
`unreported_rule_violations`. An empty finding list alone therefore never
passes. Generic escalations and ambiguous or capped findings also require
review. The adapter performs no additional model calls.

| Decision | Exit | Meaning |
|---|---:|---|
| `PASS` | 0 | Complete review and rule checklist with no reported required or advisory issues |
| `WARNING` | 0 | Advisory findings only and no reviewer escalation |
| `FAIL` | 1 | At least one classifiable required finding |
| `REVIEW_REQUIRED` | 2 | Review incomplete, inconclusive, or stale |
| `ERROR` | 3 | Configuration, provider, execution, or artifact failure |

The runner emits one sanitized JSON record with the decision, reason, rule
IDs and available run metadata. The structured review and its metadata live
in temporary files deleted on exit. When detailed logging is enabled, the
runner also logs bounded, credential-redacted copies of the model response
and structured review. The gate reviews proposed code as text.

See the [deployment trust boundary](setup-guide.md#protect-the-workflows)
and [limitations](limitations.md). [Pinned PR-Agent JSON producer](https://github.com/The-PR-Agent/pr-agent/blob/v0.50.0/pr_agent/tools/pr_reviewer.py)
