# Woodpecker setup for Gitea and GitHub

Use a dedicated repository for the demo. The operator needs administrative
access to the repository, Woodpecker 3.x connected to the forge, a reviewer
identity, and a Gemini, OpenAI, or Anthropic API project approved for the
repository's content.

## Select the CI engine

One engine runs the two gates: GitHub Actions, Woodpecker, or neither.
`ci/pipeline-engine` records the choice, and `ci/pipelines/<engine>/` holds
each engine's canonical workflows. `ci/select-pipeline.sh` installs the
selected set where that engine reads it and removes the other set:

| Selection | Installed workflows | Effect |
|---|---|---|
| `github` | `.github/workflows/build.yaml`, `semantic-review.yaml` | GitHub Actions runs. Woodpecker finds no configuration |
| `woodpecker` | `.woodpecker/build.yaml`, `semantic-review.yaml` | Woodpecker runs on Gitea or GitHub. GitHub Actions finds no workflows |
| `none` | None | No CI runs on either forge |

```sh
./ci/select-pipeline.sh                      # show the selection
./ci/select-pipeline.sh select woodpecker    # switch, then commit the result
./ci/select-pipeline.sh check                # verify the installed files
```

Edit workflows only under `ci/pipelines/`, then run `select` again to
reinstall them. Both build workflows run `check` first. The check fails if an
installed file differs from its source, a selected file is missing, the other
engine has workflows installed, or a root `.woodpecker.yaml` exists.

The selection is part of each commit, so it applies to every remote that
receives the commit, including both Gitea and GitHub. Before switching, update
the branch protection or ruleset to require the new engine's
`dotnet-gates` and `semantic-review` checks. A required check from an engine
that no longer runs keeps PRs blocked. That is also why a PR that switches to
`none` cannot merge itself past required checks. Woodpecker v3.21.0 deletes a
pipeline that has no configuration and posts no status. This applies when the
repository uses Woodpecker's default configuration path.

## Protect the workflows

Woodpecker normally reads pipeline YAML from the PR commit. For a trusted
maintainer-only demo, enable Woodpecker pipeline approval for all PRs and review
workflow changes before approving a run. Keep the repository untrusted in
Woodpecker's elevated-permissions settings. Do not preapprove fork workflows
or grant mounts, a Docker socket, privileged mode, or a shared host agent
to the semantic step.

For enforcement against untrusted authors, use an exclusive Woodpecker
configuration extension that authenticates requests and returns approved
copies of both workflows. The extension should select the repository and PR
event from trusted pipeline metadata, preserve both expected workflow names,
and enforce immutable image digests and the fixed, clone-free review step.
Reject missing or invalid configuration. Keep workflow code, secret scopes,
and policy image releases under maintainer approval. The extension is a
deployment prerequisite for that trust model. [Woodpecker security boundaries](https://woodpecker-ci.org/docs/usage/security-boundaries) · [Configuration extensions](https://woodpecker-ci.org/docs/usage/extensions/configuration-extension)

## Activate the strict review on Gitea

1. Activate the implementation repository in Woodpecker, enable pull
   requests, and verify that the conventional `build` workflow runs. Use a
   Gitea URL the worker containers can reach. The reviewer uses Woodpecker's
   `CI_FORGE_URL` for both the API and Git-over-HTTP fetch.
2. Build the reviewer image from the approved revision and publish it to the
   registry used by the runner. Match the image in the approved workflow and
   scope secrets to its exact reference. For a different registry, update the
   image reference on both steps. Use an immutable image reference for the
   deployed gate.

   ```sh
   docker build -f ci/pr-agent/Dockerfile \
     -t registry.digixoil.se/llm-ci-gate-plugin:0.4.5 .
   docker push registry.digixoil.se/llm-ci-gate-plugin:0.4.5
   ```

3. Create a Gitea personal access token for the dedicated reviewer with
   `read:repository` and `write:issue`. Inline comments also require
   `write:repository` token scope. Use a reviewer with read-only access to
   the demo. The token reads the PR, fetches its exact commits over HTTPS,
   and posts a PR conversation comment. Woodpecker uses its own forge
   credentials to publish commit status.
4. Set `gitea_review_token` and `gemini_api_key` under the Woodpecker repository's
   secrets. Enable the `pull_request` event for both and scope them to the
   reviewer image. See [Gemini setup](#use-gemini) for the model credential.
   The workflow selects the Gitea step using `CI_FORGE_TYPE`.
5. Push a fresh commit to a same-repository Gitea PR targeting `main`. Confirm
   that `build` and `semantic-review` ran, that the `semantic-review-gitea`
   step logged the expected head SHA, and that PR-Agent labeled its comment
   with that SHA. For the merge-blocking exercise, enable status checks
   on `main` and require the observed `build` and `semantic-review` contexts.
   Enable **Block merge if the pull request is outdated** and verify the
   contexts again after a PR update. [Gitea protected branches](https://docs.gitea.com/usage/access-control/protected-branches)

Use HTTPS for the token-bearing connection. An isolated local demo can use
HTTP on a trusted network. [Woodpecker Gitea setup](https://woodpecker-ci.org/docs/administration/configuration/forges/gitea) · [Gitea token configuration](https://docs.gitea.com/development/api-usage)

## Connect GitHub and Woodpecker

1. Create or select a dedicated GitHub repository and initialize `main` from
   the desired revision. The [scenario patches](demonstration-guide.md#apply-a-scenario) apply to `main`.
2. Connect Woodpecker to GitHub using its documented OAuth integration. Activate
   the repository, verify the GitHub webhook, and enable pull request events.
   GitHub `synchronize` deliveries should start a fresh run for updated commits.
3. Build the plugin from an approved revision and publish it to your registry.
   In the approved semantic workflow use the published image digest.

   ```sh
   docker build -f ci/pr-agent/Dockerfile -t llm-ci-gate-plugin:0.4.5 .
   ```

4. Use a dedicated GitHub identity limited to the demo repository. Start with a
   fine-grained token for Contents read and Pull requests write so PR-Agent can
   read the diff and post or update its PR comment. Give the token no Contents
   write, Checks write, administration, deployment, or ruleset bypass access.
5. Create Woodpecker secrets named `github_review_token` and `gemini_api_key`.
   Scope both to `pull_request` and the exact approved plugin image. Keep the
   secret-bearing step a Woodpecker plugin without `commands`, `entrypoint`, or
   `environment` overrides. See [Gemini setup](#use-gemini) for the model
   credential.

[Woodpecker GitHub integration](https://woodpecker-ci.org/docs/administration/configuration/forges/github) · [Woodpecker secret scopes](https://woodpecker-ci.org/docs/usage/secrets) · [GitHub comment permissions](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment)

## Run on GitHub Actions instead of Woodpecker

`ci/pipelines/github/` mirrors the two Woodpecker workflows for a GitHub
repository that does not use Woodpecker. Select it with
`./ci/select-pipeline.sh select github` (see [Select the CI engine](#select-the-ci-engine)).

| Workflow | Job and check name | Trigger | Equivalent |
|---|---|---|---|
| `build.yaml` | `dotnet-gates` | Push and PR to `main` | `ci/pipelines/woodpecker/build.yaml`. Checks the selection, then runs `ci/dotnet-gates.sh` in `mcr.microsoft.com/dotnet/sdk:10.0` |
| `semantic-review.yaml` | `semantic-review` | PR to `main` | `ci/pipelines/woodpecker/semantic-review.yaml`. Runs the approved reviewer image without a checkout |

1. Publish the approved reviewer image to `registry.digixoil.se`, as in the
   Woodpecker setup above. The workflow pins the same tag as the Woodpecker
   step. For a new release, update `REVIEWER_IMAGE` and both Woodpecker image
   references under `ci/pipelines/`, then reinstall the selected engine.
2. Add the repository secrets `GEMINI_API_KEY`, `REGISTRY_USERNAME` and
   `REGISTRY_PASSWORD`. Use a registry account that can only pull the reviewer
   image. For OpenAI or Claude, add `OPENAI_API_KEY` or `CLAUDE_API_KEY` and
   switch the commented provider block in the review step.
3. The review uses the job's `GITHUB_TOKEN` with `contents: read`,
   `issues: write` and `pull-requests: write`. That covers reading the PR,
   the summary comment, and the inline review. No personal token is needed.
   Comments appear as `github-actions[bot]`.
4. The job passes the PR URL and the event's head SHA to the gate's manual
   mode (`--pr-url`, `--head`). If the PR moves before the review starts, the
   gate returns `REVIEW_REQUIRED/pipeline_head_is_stale`, and the run for the
   new head replaces it. The step's exit code is the check result.
5. In the `main` ruleset, require the `dotnet-gates` and `semantic-review`
   checks with GitHub Actions as the source. Follow the verification steps in
   [Require and verify both checks](#require-and-verify-both-checks-on-github).
6. The selector installs only one engine's workflows, so Woodpecker and
   GitHub Actions cannot both review the same commit.

Fork PRs receive no secrets and a read-only token. Their semantic check fails
closed with `ERROR`, matching the gate's same-repository rule.

GitHub runs the workflow definition from the PR's merge commit. A contributor
with write access can therefore edit `.github/workflows/` or `ci/pipelines/` in a PR to skip or
replace the semantic step, and a job with the same name would still satisfy
the required check. Protect `.github/`, `.woodpecker/`, `ci/pipelines/` and `ci/pipeline-engine` with a CODEOWNERS review rule, or use an
organization ruleset that requires an approved workflow from a protected
repository. [Required workflows in rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#require-workflows-to-pass-before-merging) · [Workflow token permissions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication#permissions-for-the-github_token)

## Use Gemini

Create a Gemini Developer API key in Google AI Studio. Add it to Woodpecker
as `gemini_api_key` for `pull_request` and the exact approved 0.4.5 image
reference. Both Gitea and GitHub review steps have Gemini enabled for testing.

```yaml
settings:
  gitea_token:
    from_secret: gitea_review_token
  llm_provider: gemini
  gemini_key:
    from_secret: gemini_api_key
```

Each step includes a commented OpenAI alternative and requires its forge token
and the Gemini key when Gemini is active. The
plugin sends that key to upstream PR-Agent for the pinned
`gemini/gemini-3.5-flash-lite` model with low thinking. The
JSON report records `llm_provider` and `model`.

If Gemini returns an HTTP error, the report includes `model_http_status`
and, when recognized, `model_error_status`. For example, HTTP 400 with
`INVALID_ARGUMENT` points to request parameters, while HTTP 503 with
`UNAVAILABLE` indicates a service availability error. The log contains
only the numeric HTTP status and recognized error code from that response.
If the model client identifies where a timeout occurred, the report includes
`model_timeout_phase`. A `read` timeout means the transport stopped waiting
for response data. A `connect` timeout means it couldn't establish a connection
within the request limit.
[Gemini API troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting)

To switch providers, comment out the active provider block and uncomment
the alternative in the same step. Scope the selected secrets to the approved
image. [Gemini API keys](https://ai.google.dev/gemini-api/docs/api-key) · [PR-Agent model configuration](https://docs.pr-agent.ai/usage-guide/changing_a_model/)

## Use Claude

Create an Anthropic API key and add it to Woodpecker as `claude_api_key`.
Scope it to `pull_request` and the exact approved image. In the selected
review step, enable the commented Claude settings and comment out the
currently active provider. Keep the forge token in that step.

```yaml
llm_provider: claude
claude_key:
  from_secret: claude_api_key
```

The Claude profile uses Sonnet 5.5 with adaptive thinking. For standalone
runs, set `PLUGIN_LLM_PROVIDER=claude` and inject `PLUGIN_CLAUDE_KEY`.
[Anthropic API setup](https://platform.claude.com/docs/en/claude_api_primer) · [Claude model overview](https://platform.claude.com/docs/en/models/overview)

## Configure the reviewer

Edit [`ci/gate/settings.yaml`](../ci/gate/settings.yaml) in the approved
reviewer source and rebuild the image. The gate loads the copy installed
in the image. The Woodpecker workflow selects the provider and supplies
its credential.

- `review.effort` accepts `low`, `medium`, and `high`. The Gemini profile sends
  a thinking level, and Claude sends adaptive thinking with the corresponding
  effort. The GPT-4.1 mini profile omits reasoning effort. For a compatible
  OpenAI reasoning model, select the `openai_reasoning_effort` profile.
- `timeout_seconds.model` is the shared model request budget.
  `providers.<provider>.request_timeout_seconds` overrides it when needed.
  The model timeout must fit inside `timeout_seconds.reviewer`. The existing
  OpenAI profile keeps its 75-second timeout.
- The YAML also holds the input and log limits, model token budgets, review
  instructions, PR-Agent options, per-forge comment behavior, and benchmark
  defaults. If you change `target_branch`, update the Woodpecker branch
  filter to match.

The `review` prompt settings align the pinned PR-Agent prompt with the
approved rule catalog:

- `guidance` replaces four upstream bullets that tell the model to skip
  lower-severity concerns and prefer silence when unsure. A rule violation
  counts as a concrete issue even when the code works.
- `field_descriptions` rewrites the finding, risk, and merge recommendation
  fields so they refer to engineering rules rather than only bugs.
- `rule_checks` adds a required per-rule checklist that the model completes
  before listing findings. Without a required finding, the gate passes only
  when every catalog rule has a valid check. The PR comment shows the checklist as one
  collapsible table that names any violated rule in its summary line.
- The example finding uses `example_rule_id`. Its risk level and merge
  recommendation follow that rule's enforcement, so the example stays
  consistent with its own finding.

The default effort is `medium`. A live Gemini Flash-Lite run at `low` passed
a PR with clear ENG-004 and ENG-009 violations.

## Read semantic review logs

The Woodpecker semantic review step shows timestamped `[semantic-review]`
messages as the gate verifies the PR commits, fetches the comparison,
submits the model request, and evaluates the review. The model request
message includes its time limit. Progress is written to standard error and
the final JSON decision to standard output, so the benchmark can parse the
report. If validation blocks the gate because of an invalid review field,
the final decision includes the field names in `invalid_review_fields`.
If the gate returns `incomplete_pr_input`, look for `Comparison rejected`
in the progress log. The gate accepts up to 50 changed files and 2,000
added or deleted lines. Gitea also limits the Git patch to 1 MiB.

Both Woodpecker steps set `log_review_output: "true"`. This adds the model's
response text and structured review report to the log, with configured
credentials redacted and each entry limited to 24,000 characters. Review
text can quote the PR. Limit log access accordingly, or set the option to
`"false"` to show only progress and the decision. In standalone runs the
option defaults to off. Pass `-e PLUGIN_LOG_REVIEW_OUTPUT=true` to `docker run`
to enable it there.

## Read inline findings

`review.inline_comments` in the installed settings enables inline findings
on Gitea and GitHub. After the gate validates the review, it groups eligible
findings into a comment-only review on the pinned head. A finding needs a
recognized policy ID and an added line within its reported range. The PR
summary includes the findings regardless of inline placement.

Gitea requires `write:repository` token scope for inline PR reviews.
A read-only repository collaborator can use that scope without gaining
push access. GitHub uses **Pull requests: write**. The last JSON record
reports `inline_comments_posted` or `inline_comment_http_status` where
available. Inline publication failures leave the gate decision intact.

## Require and verify both checks on GitHub

1. Open an eligible, same-repository PR targeting `main`. In Woodpecker
   confirm both workflows ran. Compare the semantic report's head with the
   PR's head and confirm that the PR comment was published. Woodpecker v3.21.0's
   GitHub integration uses the PR head SHA from the webhook and publishes a
   commit status to that SHA. [Webhook parser](https://github.com/woodpecker-ci/woodpecker/blob/v3.21.0/server/forge/github/parse.go) · [GitHub status publisher](https://github.com/woodpecker-ci/woodpecker/blob/v3.21.0/server/forge/github/github.go)
2. Read the exact status names and identities GitHub received. For instance,
   with an already-authenticated GitHub CLI, query both status APIs for the PR
   head.

   ```sh
   gh api repos/OWNER/REPO/commits/HEAD_SHA/status --jq '.statuses[] | [.context,.state,.creator.login]'
   gh api repos/OWNER/REPO/commits/HEAD_SHA/check-runs --jq '.check_runs[] | [.name,.conclusion,.app.slug]'
   ```

3. In a `main` branch ruleset require a PR, require the observed conventional
   and semantic contexts independently, and select the expected check source
   where offered. Turn on the up-to-date branch requirement. Review bypass
   actors and verify ordinary contributor direct pushes are denied.
4. Verify that a build failure and a semantic violation each independently
   block merge. Remove either check on a disposable PR by leaving it pending
   and verify that merging remains blocked. After a fix, verify both contexts
   use the new head. Also verify a PR push and retarget trigger the expected
   workflow behavior in your instance. Woodpecker v3.21.0 treats edits,
   including retargeting, as `pull_request_metadata`, so reopen the PR or push a
   fresh commit if a retargeted PR needs a new `pull_request` run. Re-evaluate open
   PRs when a new protected policy image is deployed.

[GitHub rulesets and expected check sources](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#require-status-checks-to-pass-before-merging)
