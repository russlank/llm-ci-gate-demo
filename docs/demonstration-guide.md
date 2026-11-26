# Demonstration guide

## Apply a scenario

Scenarios are small changes to the commit tagged `scenarios-v1`, each on its
own `scenario/<ID>-<slug>` branch. The [scenario catalog](../scenarios/README.md) lists them with
expected outcomes and shows how to build a branch in a temporary worktree.
Push the branch to the repository whose CI engine is selected (see
`./ci/select-pipeline.sh`), then open a PR targeting `main`.

Start with SC01, SC02 and SC07 for a clean, a blocking and an advisory result.
SC17 shows a misspelling that the tests accept but ENG-009 blocks, and SC18
shows a changed default output (ENG-008). For SC09 and SC14, push the second
commit to the same PR after the first checks finish. Never merge a scenario
branch, and delete it afterwards.

To confirm a scenario's conventional result before a live run, build its
branch locally:

```sh
git worktree add ../scenario-check scenario/SC02-magic-string
(cd ../scenario-check && ./ci/dotnet-gates.sh)
git worktree remove ../scenario-check
```

## Run the pinned reviewer manually

Use the same trusted image as Woodpecker. Inject the two credentials into the
environment from your secret store. The reviewer writes to the selected
PR and makes a provider call.

```sh
docker run --rm -e PLUGIN_GITHUB_TOKEN -e PLUGIN_OPENAI_KEY \
  llm-ci-gate-plugin:0.4.5 \
  --pr-url https://github.com/OWNER/REPO/pull/123 --head FULL_HEAD_SHA
```

For Gitea, supply the base URL and use the Gitea token instead.

```sh
docker run --rm -e PLUGIN_GITEA_TOKEN -e PLUGIN_OPENAI_KEY \
  llm-ci-gate-plugin:0.4.5 --gitea-url https://gitea.example.test \
  --pr-url https://gitea.example.test/OWNER/REPO/pulls/123 --head FULL_HEAD_SHA
```

For a Gitea PR reviewed by Gemini, inject `PLUGIN_GEMINI_KEY` and set the
provider to `gemini`.

```sh
docker run --rm -e PLUGIN_GITEA_TOKEN -e PLUGIN_GEMINI_KEY \
  -e PLUGIN_LLM_PROVIDER=gemini llm-ci-gate-plugin:0.4.5 \
  --gitea-url https://gitea.example.test \
  --pr-url https://gitea.example.test/OWNER/REPO/pulls/123 --head FULL_HEAD_SHA
```

The command prints a sanitized JSON decision and exits with the same code as
the plugin. Omit `--head` to evaluate the current PR head interactively.
For Claude, use `-e PLUGIN_CLAUDE_KEY -e PLUGIN_LLM_PROVIDER=claude` in the
same command.

## Measure the scenarios

With live credentials, run each applicable scenario three times on the
same head. Record expected IDs from the scenario
catalog and use the provider's input and output rates if cost estimates are
needed. GitHub can update the same comment across repetitions. Gitea gets a separate comment for each run. For Gitea, pass its PR URL and `--gitea-url` to the benchmark as well.
For Gemini, inject `PLUGIN_GEMINI_KEY` and pass `--llm-provider gemini`.
For Claude, inject `PLUGIN_CLAUDE_KEY` and pass `--llm-provider claude`.
Install the host-side YAML dependency before running the benchmark.

```sh
mkdir -p out
python3 -m pip install -r ci/gate/requirements.txt
python3 ci/gate/benchmark.py SC02 https://github.com/OWNER/REPO/pull/123 \
  FULL_HEAD_SHA FAIL --rule ENG-001 > out/sc02.jsonl
```

For SC11 and SC12, also run the offline tests to check the gate's response
without a provider. Track model-detection accuracy separately from the
adapter's decision for a known input. Keep missing model outcomes out of
false-positive and false-negative counts.
