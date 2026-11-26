"""Supply PR-Agent with the exact Git comparison for a Gitea pull request."""

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

from giteapy.rest import ApiException
from pr_agent.algo.language_handler import is_valid_file
from pr_agent.git_providers.diff_parsing import parse_unified_diff, to_hunk_only_patch
from pr_agent.git_providers.gitea_provider import GiteaProvider
from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from settings import load_settings


class IncompleteInput(Exception):
    """The comparison cannot provide complete review input."""


class GiteaGitFetchError(Exception):
    """The runner could not fetch the pinned commits from Gitea."""


class GiteaCommentError(Exception):
    """Gitea did not accept or confirm a review comment."""


def validate_comparison_size(file_count, changed_lines):
    """Enforce the shared comparison limits before model review."""
    limits = load_settings()["limits"]
    if not 0 < file_count <= limits["changed_files"]:
        raise IncompleteInput(f"Changed-file count {file_count} must be between 1 and {limits['changed_files']}")
    if changed_lines > limits["changed_lines"]:
        raise IncompleteInput(f"Changed-line count {changed_lines} exceeds the limit of {limits['changed_lines']}")


def run_git(repo, *args, auth=None):
    """Run bounded Git commands without ambient configuration or interactive prompts."""
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if auth:
        environment.update(
            GIT_CONFIG_COUNT="3",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {auth}",
            GIT_CONFIG_KEY_1="http.followRedirects",
            GIT_CONFIG_VALUE_1="false",
            GIT_CONFIG_KEY_2="protocol.allow",
            GIT_CONFIG_VALUE_2="never",
            GIT_ALLOW_PROTOCOL="http:https",
        )
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo), *args],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=load_settings()["timeout_seconds"]["git"],
        check=True,
    ).stdout


def prepare_gitea_comparison(working_directory, clone_url, token, base_sha, head_sha):
    """Fetch the two immutable commits into a disposable bare repository."""
    # Gitea supports a PAT as the password for Git Smart HTTP. Keep it out of
    # the URL, process arguments, Git config files and CI logs.
    auth = base64.b64encode(f"pr-agent:{token}".encode()).decode("ascii")
    with tempfile.TemporaryDirectory(prefix="gitea-git-", dir=working_directory) as directory:
        repo = Path(directory)
        run_git(repo, "init", "--bare", "--template=")
        try:
            run_git(repo, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                    clone_url, base_sha, head_sha, auth=auth)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise GiteaGitFetchError from error
        return read_comparison(repo, base_sha, head_sha)


def read_comparison(repo, base_sha, head_sha):
    """Verify every textual hunk against Git's independent changed-file inventory."""
    max_bytes = load_settings()["limits"]["git_patch_bytes"]
    try:
        bases = run_git(repo, "merge-base", "--all", base_sha, head_sha).decode("ascii").splitlines()
    except subprocess.CalledProcessError as error:
        raise IncompleteInput("No usable merge base") from error
    if len(bases) != 1:
        raise IncompleteInput("Comparison has multiple merge bases")

    options = ("--no-renames", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none")
    # A merge-base-to-head comparison excludes changes confined to the base branch.
    revisions = (bases[0], head_sha, "--")
    inventory = run_git(repo, "diff", *options, "--numstat", "-z", *revisions)
    if not inventory or len(inventory) > max_bytes:
        raise IncompleteInput("Missing or oversized changed-file inventory")

    stats = {}
    try:
        for record in inventory.decode("utf-8").rstrip("\0").split("\0"):
            added, deleted, path = record.split("\t", 2)
            counts = int(added), int(deleted)
            if not sum(counts) or path in stats:
                raise IncompleteInput("Comparison has a metadata-only or duplicate entry")
            stats[path] = counts
    except ValueError as error:
        raise IncompleteInput("Comparison has a binary or unreadable entry") from error

    validate_comparison_size(len(stats), sum(sum(counts) for counts in stats.values()))

    diff = run_git(repo, "diff", *options, "--no-color", "--src-prefix=a/", "--dst-prefix=b/", *revisions)
    if len(diff) > max_bytes:
        raise IncompleteInput("Comparison exceeds the patch size limit")
    try:
        files = parse_unified_diff(diff.decode("utf-8"))
    except (UnidiffParseError, UnicodeError) as error:
        raise IncompleteInput("Comparison has an unreadable patch") from error

    if len(files) != len(stats) or {file.filename for file in files} != stats.keys():
        raise IncompleteInput("Patch inventory differs from Git's changed-file inventory")
    for file in files:
        patch = to_hunk_only_patch(file.patch)
        lines = patch.split("\n")
        counts = sum(line.startswith("+") for line in lines), sum(line.startswith("-") for line in lines)
        if not patch.startswith("@@ ") or counts != stats[file.filename]:
            raise IncompleteInput("Patch review coverage is incomplete")
        file.patch = patch
        file.num_plus_lines, file.num_minus_lines = counts
        file.head_file_is_complete = False
    return files


def added_line_numbers(files):
    """Collect right-side anchors from the same verified patches given to PR-Agent."""
    anchors = {}
    for file in files:
        if not file.patch:
            continue
        patch = to_hunk_only_patch(file.patch)
        try:
            parsed = PatchSet(f"--- a/file\n+++ b/file\n{patch}")
        except UnidiffParseError:
            continue
        lines = [line.target_line_no for changed in parsed for hunk in changed for line in hunk if line.is_added]
        if lines:
            anchors[file.filename] = lines
    return anchors


def create_gitea_provider(head_sha, base_sha, snapshot_path, progress):
    """Bind PR-Agent's Gitea provider to the verified comparison and head-labeled output."""
    class PinnedGiteaProvider(GiteaProvider):
        incomplete = False

        def _get_changed_files(self):
            if self.git_files is None:
                try:
                    self._load_comparison()
                except IncompleteInput as error:
                    PinnedGiteaProvider.incomplete = True
                    progress(f"Comparison rejected ({error})")
                    raise
            return self.git_files

        def _load_comparison(self):
            if self.sha != head_sha or self.base_sha != base_sha:
                raise IncompleteInput("PR identity changed before the review")
            progress("Fetching the pinned Gitea comparison over Git")
            files = prepare_gitea_comparison(
                Path.cwd(), self.get_git_repo_url(self.pr_url), self.gitea_access_token, base_sha, head_sha
            )
            if any(not is_valid_file(file.filename) for file in files):
                raise IncompleteInput("Comparison contains files excluded by PR-Agent")
            changed_lines = sum(file.num_plus_lines + file.num_minus_lines for file in files)
            progress(f"Validated the Gitea comparison with {len(files)} changed files and {changed_lines} changed lines")
            self.git_files = [{"filename": file.filename} for file in files]
            self.diff_files = files

        def get_diff_files(self):
            self._get_changed_files()
            return self.diff_files

        def publish_comment(self, comment, is_temporary=False):
            try:
                result = super().publish_comment(f"Review target: `{head_sha}`\n\n{comment}", is_temporary)
            except ApiException as error:
                raise GiteaCommentError from error
            if not is_temporary and not result:
                raise GiteaCommentError
            return result

        def publish_structured_review(self, _report):
            files = self.get_diff_files()
            snapshot = {
                "head_sha": self.sha,
                "base_sha": self.base_sha,
                "changed_files": self.get_files(),
                "review_files": [file.filename for file in files],
            }
            if load_settings()["review"]["inline_comments"]:
                snapshot["added_lines"] = added_line_numbers(files)
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    return PinnedGiteaProvider
