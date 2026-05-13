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
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RemoteState:
    name: str
    url: str
    branch: str
    sha: str | None


BUILT_IN_REGEX_PATTERNS = [
    ("GitHub token", rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,255}\b"),
    ("GitHub fine-grained token", rb"\bgithub_pat_[A-Za-z0-9_]{20,500}\b"),
    ("Stripe secret key", rb"\b(?:sk|rk)_(?:test|live)_[A-Za-z0-9_]{8,255}\b"),
]


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


def run_bytes(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    result = run_quiet(["git", "rev-parse", "--show-toplevel"], path, capture=True)
    return Path(result.stdout.strip()).resolve()


def current_branch(repo: Path) -> str:
    result = run_quiet(["git", "branch", "--show-current"], repo, capture=True)
    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("Detached HEAD is not supported by this script.")
    return branch


def remote_url(repo: Path, remote: str) -> str | None:
    result = run_quiet(["git", "remote", "get-url", remote], repo, check=False, capture=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def remote_branch_sha(repo: Path, remote: str, branch: str) -> str | None:
    result = run_quiet(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        repo,
        check=False,
        capture=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def ensure_clean_worktree(repo: Path) -> None:
    result = run_quiet(["git", "status", "--porcelain"], repo, capture=True)
    if result.stdout.strip():
        print(result.stdout)
        if not ask_yes_no("Worktree is dirty. Continue anyway?", default=False):
            raise SystemExit("Aborted because the worktree is dirty.")


def ensure_filter_repo(repo: Path) -> None:
    result = run_quiet(["git", "filter-repo", "--version"], repo, check=False, capture=True)
    if result.returncode == 0:
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


def write_replacements(patterns: list[str], include_built_ins: bool) -> Path | None:
    if not patterns and not include_built_ins:
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
        if include_built_ins:
            for _, pattern in BUILT_IN_REGEX_PATTERNS:
                temp.write(f"regex:{pattern.decode('ascii')}\n")
    return Path(temp.name)


def build_filter_repo_command(replacement_file: Path | None, paths: list[str]) -> list[str]:
    command = ["git", "filter-repo", "--force"]
    if replacement_file is not None:
        command.extend(["--replace-text", str(replacement_file)])
        command.extend(["--replace-message", str(replacement_file)])
    for path in paths:
        command.extend(["--path", path])
    if paths:
        command.append("--invert-paths")
    return command


def verify_patterns_absent(
    repo: Path,
    patterns: list[str],
    *,
    include_built_ins: bool,
) -> bool:
    if not patterns and not include_built_ins:
        return True
    exact_patterns = encoded_patterns(patterns)
    regex_patterns = built_in_regex_patterns() if include_built_ins else []
    found = False
    if scan_commit_messages(repo, exact_patterns, regex_patterns):
        found = True
    objects = reachable_blob_paths(repo)
    if not objects:
        return not found
    for blob, paths in objects.items():
        result = run_bytes(
            ["git", "cat-file", "blob", blob],
            repo,
            check=False,
        )
        if result.returncode != 0:
            print(result.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
            return False
        data = result.stdout
        for pattern, needle in exact_patterns:
            if needle in data:
                found = True
                display_paths = ", ".join(paths[:5])
                if len(paths) > 5:
                    display_paths += f", ... ({len(paths)} paths)"
                print(f"Found pattern still present in blob {blob}: {display_paths}")
        for label, regex in regex_patterns:
            if regex.search(data):
                found = True
                display_paths = ", ".join(paths[:5])
                if len(paths) > 5:
                    display_paths += f", ... ({len(paths)} paths)"
                print(f"Found {label} still present in blob {blob}: {display_paths}")
    return not found


def scan_commit_messages(
    repo: Path,
    exact_patterns: list[tuple[str, bytes]],
    regex_patterns: list[tuple[str, re.Pattern[bytes]]],
) -> bool:
    result = run_bytes(
        ["git", "log", "--all", "--format=%H%x00%B%x00"],
        repo,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
        return True
    data = result.stdout
    found = False
    for _, needle in exact_patterns:
        if needle in data:
            found = True
            print("Found exact pattern still present in commit messages.")
    for label, regex in regex_patterns:
        if regex.search(data):
            found = True
            print(f"Found {label} still present in commit messages.")
    return found


def built_in_regex_patterns() -> list[tuple[str, re.Pattern[bytes]]]:
    return [(label, re.compile(pattern)) for label, pattern in BUILT_IN_REGEX_PATTERNS]


def encoded_patterns(patterns: list[str]) -> list[tuple[str, bytes]]:
    encoded: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for pattern in patterns:
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            needle = pattern.encode(encoding)
            if needle and needle not in seen:
                seen.add(needle)
                encoded.append((pattern, needle))
    return encoded


def verify_paths_absent(repo: Path, paths: list[str]) -> bool:
    if not paths:
        return True
    wanted = set(paths)
    objects = reachable_blob_paths(repo)
    found = False
    for blob, blob_paths in objects.items():
        for path in blob_paths:
            if path in wanted:
                found = True
                print(f"Found path still present in blob {blob}: {path}")
    return not found


def reachable_blob_paths(repo: Path) -> dict[str, list[str]]:
    result = run_quiet(["git", "rev-list", "--objects", "--all"], repo, capture=True)
    blob_paths: dict[str, list[str]] = defaultdict(list)
    for line in result.stdout.splitlines():
        if not line:
            continue
        oid, _, path = line.partition(" ")
        if not path:
            continue
        kind = run_quiet(["git", "cat-file", "-t", oid], repo, capture=True)
        if kind.stdout.strip() == "blob":
            blob_paths[oid].append(path)
    return dict(blob_paths)


def warn_missing_paths(repo: Path, paths: list[str]) -> None:
    if not paths:
        return
    existing_paths = {
        path
        for blob_paths in reachable_blob_paths(repo).values()
        for path in blob_paths
    }
    for path in paths:
        if path not in existing_paths:
            print(f"Warning: path was not found in reachable history: {path}")


def restore_remote(repo: Path, remote: RemoteState) -> None:
    if remote_url(repo, remote.name) == remote.url:
        return
    if remote_url(repo, remote.name) is not None:
        run_quiet(["git", "remote", "remove", remote.name], repo)
    run_quiet(["git", "remote", "add", remote.name, remote.url], repo)


def push_rewritten_history(repo: Path, remote: RemoteState) -> None:
    restore_remote(repo, remote)
    if remote.sha:
        lease = f"refs/heads/{remote.branch}:{remote.sha}"
        run_quiet(
            ["git", "push", f"--force-with-lease={lease}", "-u", remote.name, remote.branch],
            repo,
        )
    else:
        run_quiet(["git", "push", "--force", "-u", remote.name, remote.branch], repo)


def main() -> int:
    if shutil.which("git") is None:
        raise SystemExit("git was not found on PATH.")

    repo_input = ask("Repository path", os.getcwd())
    repo = repo_root(Path(repo_input).expanduser().resolve())
    branch = current_branch(repo)
    print(f"Repository: {repo}")

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
        print(f"Remote: {remote.name}")
    else:
        print(f"Remote {remote_name!r} not found; push step will be skipped.")

    patterns = collect_secret_patterns()
    paths = collect_paths()
    include_built_ins = True
    if not patterns and not paths and not include_built_ins:
        raise SystemExit("Nothing to remove. Add at least one secret pattern or path.")

    print("\nAbout to rewrite Git history.")
    print(f"Secret patterns: {len(patterns)}")
    print("Built-in token formats: GitHub tokens, Stripe sk_test/sk_live")
    print(f"Paths to remove: {', '.join(paths) if paths else 'none'}")
    warn_missing_paths(repo, paths)
    if not ask_yes_no("Continue with git filter-repo?", default=False):
        raise SystemExit("Aborted before rewriting history.")

    replacement_file = write_replacements(patterns, include_built_ins)
    try:
        command = build_filter_repo_command(replacement_file, paths)
        print("Running git filter-repo cleanup...", flush=True)
        run_quiet(command, repo)
    finally:
        if replacement_file is not None:
            replacement_file.unlink(missing_ok=True)

    print("\nVerification: searching all reachable commits.")
    patterns_absent = verify_patterns_absent(
        repo,
        patterns,
        include_built_ins=include_built_ins,
    )
    paths_absent = verify_paths_absent(repo, paths)
    if not patterns_absent:
        print("One or more patterns are still present. Review output before pushing.")
        return 2
    if not paths_absent:
        print("One or more removed paths are still present. Review output before pushing.")
        return 2
    print("No matching secret patterns or removed paths were found in reachable history.")

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
