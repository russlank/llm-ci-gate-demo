import subprocess
import tempfile
import unittest
from pathlib import Path

from gitea_compare import IncompleteInput, read_comparison
from settings import load_settings


class GiteaComparisonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "CI fixture")
        self.git("config", "user.email", "fixture@example.test")
        self.commit_file("Greeting.cs", b"var greeting = GreetingMessages.Default;\n")
        self.git("switch", "-c", "feature")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True, check=True, text=True
        ).stdout.strip()

    def commit_file(self, path, content):
        (self.repo / path).write_bytes(content)
        self.git("add", "--", path)
        self.git("commit", "-m", "Update fixture")
        return self.git("rev-parse", "HEAD")

    def test_diff_excludes_commits_added_only_to_the_base_branch(self):
        head = self.commit_file("Greeting.cs", b'var greeting = "Hello, World!";\n')
        self.git("switch", "main")
        base = self.commit_file("README.md", b"Updated on main\n")

        files = read_comparison(self.repo, base, head)
        self.assertEqual([file.filename for file in files], ["Greeting.cs"])
        self.assertIn("-var greeting = GreetingMessages.Default;", files[0].patch)
        self.assertIn('+var greeting = "Hello, World!";', files[0].patch)
        self.assertEqual((files[0].num_plus_lines, files[0].num_minus_lines), (1, 1))

    def test_binary_change_cannot_disappear_from_the_review(self):
        base = self.git("rev-parse", "main")
        head = self.commit_file("binary.dat", b"\0binary\0")

        with self.assertRaises(IncompleteInput):
            read_comparison(self.repo, base, head)

    def test_comparison_accepts_and_enforces_the_changed_line_limit(self):
        base = self.git("rev-parse", "HEAD")
        limit = load_settings()["limits"]["changed_lines"]
        lines = b"// inserted line\n" * limit
        at_limit = self.commit_file("Example.cs", lines)
        above_limit = self.commit_file("Example.cs", lines + b"// one more\n")

        files = read_comparison(self.repo, base, at_limit)
        self.assertEqual(files[0].num_plus_lines, limit)
        with self.assertRaisesRegex(IncompleteInput, f"Changed-line count {limit + 1}"):
            read_comparison(self.repo, base, above_limit)

    def test_unrelated_histories_cannot_supply_a_review(self):
        base = self.git("rev-parse", "main")
        self.git("switch", "--orphan", "unrelated")
        head = self.commit_file("Other.cs", b"unrelated\n")

        with self.assertRaises(IncompleteInput):
            read_comparison(self.repo, base, head)


if __name__ == "__main__":
    unittest.main()
