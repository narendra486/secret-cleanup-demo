#!/usr/bin/env python3
"""Interactively remove committed secrets with git-filter-repo.

Requires:
  - Python 3.10+; compatible with Python 3.14
  - git
  - git-filter-repo installed as `git filter-repo`

Recommended usage:
  1. Rotate/revoke the leaked secret first.
  2. Run this script from a fresh clone.
  3. Let the script rewrite history.
  4. Force-push only after reviewing the verification output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RemoteState:
    name: str
    url: str
    branch: str
    sha: str | None


def run(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {format_command(args)}")
    return run_quiet(args, cwd, check=check, capture=capture)


def run_quiet(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
    )


def format_command(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return " ".join(args)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    if value:
        return value
    if default is not None:
        return default
    return ""


def ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def repo_root(path: Path) -> Path:
    result = run(["git", "rev-parse", "--show-toplevel"], path, capture=True)
    return Path(result.stdout.strip()).resolve()


def current_branch(repo: Path) -> str:
    result = run(["git", "branch", "--show-current"], repo, capture=True)
    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("Detached HEAD is not supported by this script.")
    return branch


def remote_url(repo: Path, remote: str) -> str | None:
    result = run(["git", "remote", "get-url", remote], repo, check=False, capture=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def remote_branch_sha(repo: Path, remote: str, branch: str) -> str | None:
    result = run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        repo,
        check=False,
        capture=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def ensure_clean_worktree(repo: Path) -> None:
    result = run(["git", "status", "--porcelain"], repo, capture=True)
    if result.stdout.strip():
        print(result.stdout)
        if not ask_yes_no("Worktree is dirty. Continue anyway?", default=False):
            raise SystemExit("Aborted because the worktree is dirty.")


def ensure_filter_repo(repo: Path) -> None:
    result = run(["git", "filter-repo", "--version"], repo, check=False, capture=True)
    if result.returncode == 0:
        print(result.stdout.strip())
        return
    raise SystemExit(
        "git-filter-repo was not found.\n"
        "Install it first, for example:\n"
        "  python3 -m pipx install git-filter-repo\n"
        "or see https://github.com/newren/git-filter-repo"
    )


def collect_secret_patterns() -> list[str]:
    print("\nEnter secret values or patterns to replace with ***REMOVED***.")
    print("Press Enter on an empty prompt when done.")
    patterns: list[str] = []
    seen: set[str] = set()
    while True:
        value = ask("Secret/pattern")
        if not value:
            break
        if value in seen:
            print("Already added; skipping duplicate.")
            continue
        seen.add(value)
        patterns.append(value)
    return patterns


def collect_paths() -> list[str]:
    print("\nEnter committed file paths to remove from all history, such as .env.")
    print("Press Enter on an empty prompt when done.")
    paths: list[str] = []
    seen: set[str] = set()
    while True:
        value = ask("Path to remove")
        if not value:
            break
        value = value.replace("\\", "/").lstrip("/")
        if value in seen:
            print("Already added; skipping duplicate.")
            continue
        seen.add(value)
        paths.append(value)
    return paths


def write_replacements(patterns: list[str]) -> Path | None:
    if not patterns:
        return None
    temp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="git-filter-repo-replacements-",
        suffix=".txt",
        delete=False,
    )
    with temp:
        for pattern in patterns:
            temp.write(f"{pattern}\n")
    return Path(temp.name)


def build_filter_repo_command(replacement_file: Path | None, paths: list[str]) -> list[str]:
    command = ["git", "filter-repo", "--force"]
    if replacement_file is not None:
        command.extend(["--replace-text", str(replacement_file)])
    for path in paths:
        command.extend(["--path", path])
    if paths:
        command.append("--invert-paths")
    return command


def verify_patterns_absent(repo: Path, patterns: list[str]) -> bool:
    if not patterns:
        return True
    revs = run(["git", "rev-list", "--all"], repo, capture=True).stdout.splitlines()
    found = False
    for pattern in patterns:
        for rev in revs:
            result = subprocess.run(
                ["git", "grep", "-F", "-n", "--", pattern, rev],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                found = True
                print(result.stdout, end="")
    return not found


def warn_missing_paths(repo: Path, paths: list[str]) -> None:
    if not paths:
        return
    revs = run(["git", "rev-list", "--all"], repo, capture=True).stdout.splitlines()
    for path in paths:
        found = False
        for rev in revs:
            result = run_quiet(
                ["git", "cat-file", "-e", f"{rev}:{path}"],
                repo,
                check=False,
                capture=True,
            )
            if result.returncode == 0:
                found = True
                break
        if not found:
            print(f"Warning: path was not found in reachable history: {path}")


def restore_remote(repo: Path, remote: RemoteState) -> None:
    if remote_url(repo, remote.name) == remote.url:
        return
    if remote_url(repo, remote.name) is not None:
        run(["git", "remote", "remove", remote.name], repo)
    run(["git", "remote", "add", remote.name, remote.url], repo)


def push_rewritten_history(repo: Path, remote: RemoteState) -> None:
    restore_remote(repo, remote)
    if remote.sha:
        lease = f"refs/heads/{remote.branch}:{remote.sha}"
        run(
            ["git", "push", f"--force-with-lease={lease}", "-u", remote.name, remote.branch],
            repo,
        )
    else:
        run(["git", "push", "--force", "-u", remote.name, remote.branch], repo)


def main() -> int:
    if shutil.which("git") is None:
        raise SystemExit("git was not found on PATH.")

    repo_input = ask("Repository path", os.getcwd())
    repo = repo_root(Path(repo_input).expanduser().resolve())
    branch = current_branch(repo)
    print(f"Repository: {repo}")
    print(f"Branch: {branch}")

    ensure_clean_worktree(repo)
    ensure_filter_repo(repo)

    remote_name = ask("Remote name", "origin")
    url = remote_url(repo, remote_name)
    remote = None
    if url:
        remote = RemoteState(
            name=remote_name,
            url=url,
            branch=branch,
            sha=remote_branch_sha(repo, remote_name, branch),
        )
        print(f"Remote: {remote.name} -> {remote.url}")
        print(f"Remote branch SHA: {remote.sha or 'not found'}")
    else:
        print(f"Remote {remote_name!r} not found; push step will be skipped.")

    patterns = collect_secret_patterns()
    paths = collect_paths()
    if not patterns and not paths:
        raise SystemExit("Nothing to remove. Add at least one secret pattern or path.")

    print("\nAbout to rewrite Git history.")
    print(f"Secret patterns: {len(patterns)}")
    print(f"Paths to remove: {', '.join(paths) if paths else 'none'}")
    warn_missing_paths(repo, paths)
    if not ask_yes_no("Continue with git filter-repo?", default=False):
        raise SystemExit("Aborted before rewriting history.")

    replacement_file = write_replacements(patterns)
    try:
        command = build_filter_repo_command(replacement_file, paths)
        print("+ git filter-repo --force [redacted cleanup arguments]")
        run_quiet(command, repo)
    finally:
        if replacement_file is not None:
            replacement_file.unlink(missing_ok=True)

    print("\nVerification: searching all reachable commits.")
    if verify_patterns_absent(repo, patterns):
        print("No provided secret patterns were found in reachable history.")
    else:
        print("One or more patterns are still present. Review output before pushing.")
        return 2

    if remote and ask_yes_no("Force-push rewritten history to GitHub/remote?", default=False):
        push_rewritten_history(repo, remote)
        print("Rewritten history pushed.")
        print("Ask collaborators to re-clone or hard-reset to the rewritten branch.")
    else:
        print("Push skipped.")

    print("\nImportant: rotate/revoke any real leaked secrets even after cleanup.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print(f"Command failed with exit code {error.returncode}: {error.cmd}", file=sys.stderr)
        if error.stdout:
            print(error.stdout, file=sys.stderr)
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        raise SystemExit(error.returncode)
