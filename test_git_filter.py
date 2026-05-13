from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def load_module():
    path = Path(__file__).with_name("git-filter.py")
    spec = importlib.util.spec_from_file_location("git_filter_script", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


git_filter = load_module()


class GitFilterTests(unittest.TestCase):
    def test_build_filter_repo_command_includes_messages_and_paths(self):
        command = git_filter.build_filter_repo_command(
            git_filter.FilterRepoCommand(
                args=["git-filter-repo"],
                display_name="git-filter-repo",
            ),
            Path("replacements.txt"),
            [".env", "config/prod.env"],
        )

        self.assertEqual(command[0], "git-filter-repo")
        self.assertIn("--replace-text", command)
        self.assertIn("--replace-message", command)
        self.assertIn("--invert-paths", command)
        self.assertEqual(command.count("--path"), 2)

    def test_restore_remote_readds_origin_after_filter_repo_removes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            remote = git_filter.RemoteState(
                name="origin",
                url="https://github.com/example/security-test.git",
                branch="main",
                sha="abc123",
            )

            git_filter.restore_remote(repo, remote)
            url = git_filter.remote_url(repo, "origin")

            self.assertEqual(url, remote.url)

    def test_restore_remote_replaces_wrong_origin_url(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/wrong/repo.git"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            remote = git_filter.RemoteState(
                name="origin",
                url="https://github.com/example/security-test.git",
                branch="main",
                sha="abc123",
            )

            git_filter.restore_remote(repo, remote)
            url = git_filter.remote_url(repo, "origin")

            self.assertEqual(url, remote.url)

    def test_prefix_replacement_file_uses_full_token_regex_not_literal_prefix(self):
        replacement_file = git_filter.write_replacements(["sk_live"], include_built_ins=False)
        try:
            content = replacement_file.read_text(encoding="utf-8")
        finally:
            replacement_file.unlink(missing_ok=True)

        self.assertIn("regex:sk_live_[A-Za-z0-9_]{8,255}", content)
        self.assertNotIn("\nsk_live\n", f"\n{content}\n")

    def test_encoded_patterns_include_windows_utf16_forms(self):
        encoded = [needle for _, needle in git_filter.encoded_patterns(["dummy_api_key_123456"])]

        self.assertIn("dummy_api_key_123456".encode("utf-8"), encoded)
        self.assertIn("dummy_api_key_123456".encode("utf-16-le"), encoded)
        self.assertIn("dummy_api_key_123456".encode("utf-16-be"), encoded)

    def test_reachable_blob_paths_uses_git_history(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            (repo / "secret.txt").write_text("dummy_api_key_123456\n", encoding="utf-8")
            subprocess.run(["git", "add", "secret.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Add secret file"],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            blobs = git_filter.reachable_blob_paths(repo)

            self.assertTrue(any("secret.txt" in paths for paths in blobs.values()))

    def test_repo_root_rejects_non_git_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(SystemExit) as error:
                git_filter.repo_root(Path(temp))

            self.assertIn("not inside a Git repository", str(error.exception))

    def test_reachable_blob_paths_raises_for_non_git_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError) as error:
                git_filter.reachable_blob_paths(Path(temp))

            self.assertIn("Unable to list Git objects", str(error.exception))


if __name__ == "__main__":
    unittest.main()
