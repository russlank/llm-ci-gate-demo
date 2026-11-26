# LLM CI Gate Demo

HelloWorld is a small .NET 10 console app for demonstrating how natural-language
engineering rules can complement conventional CI. Its default output is
`Hello, World!`. Five optional greeting features ship disabled.

```sh
./ci/dotnet-gates.sh
dotnet run --project src/HelloWorld
HELLOWORLD_FeatureManagement__UppercaseGreeting=true dotnet run --project src/HelloWorld
python3 -m pip install -r ci/gate/requirements.txt
PYTHONPATH=ci/gate python3 -m unittest discover -s ci/gate/tests -v
./ci/select-pipeline.sh            # CI engine: github, woodpecker, or none
```

The offline checks require the .NET 10 SDK and Python 3.12 or newer. The live
demonstration also uses Docker, Woodpecker 3.x or GitHub Actions, Gitea or GitHub, and credentials
for OpenAI, Gemini, or Claude. Reviewer settings are in
[`ci/gate/settings.yaml`](ci/gate/settings.yaml).

- [Architecture and configuration](docs/architecture.md)
- [Gitea, GitHub, and Woodpecker setup](docs/setup-guide.md), including the
  [GitHub Actions variant](docs/setup-guide.md#run-on-github-actions-instead-of-woodpecker) and the
  [CI engine selection](docs/setup-guide.md#select-the-ci-engine)
- [Demonstration and benchmark](docs/demonstration-guide.md)
- [Operational limitations and native PR-Agent comparison](docs/limitations.md)
- [Observed results](docs/evaluation-results.md)
- [Engineering rules](policies/engineering-rules.md) and [scenario patches](scenarios/README.md)
