FROM pragent/pr-agent:0.50.0@sha256:8978ae8f025883159ccc6f5bec51e6553347c77d14dbc5ca4bf3bb4a8726eb43

# Package the approved runtime, settings, and policy independently of a PR checkout.
WORKDIR /opt/llm-ci-gate
COPY policies/ ./policies/
COPY ci/gate/evaluate.py ci/gate/run.py ci/gate/settings.py ci/gate/settings.yaml ./ci/gate/
COPY ci/pr-agent/review.py ci/pr-agent/gitea_compare.py ci/pr-agent/entrypoint.sh ./ci/pr-agent/
USER 10001:10001
ENTRYPOINT ["/opt/llm-ci-gate/ci/pr-agent/entrypoint.sh"]
