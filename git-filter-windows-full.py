#!/usr/bin/env python3
"""Windows standalone secret cleanup script for Git repositories.

Requires:
  - Python 3.10+; compatible with Python 3.14
  - Git for Windows
  - git-filter-repo installed as `git filter-repo` or `git-filter-repo`

PowerShell usage:
  py -3 git-filter-windows-full.py

Recommended incident flow:
  1. Rotate/revoke the leaked secret first.
  2. Run this script from a fresh clone.
  3. Let the script rewrite history.
  4. Let the script restore origin and force-push after verification.
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
from typing import Callable


@dataclass(frozen=True)
class RemoteState:
    name: str
    url: str
    branch: str
    sha: str | None


@dataclass(frozen=True)
class FilterRepoCommand:
    args: list[str]
    display_name: str


BUILT_IN_REGEX_PATTERNS = [
    ("GitHub token", rb"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,255}"),
    ("GitHub fine-grained token", rb"github_pat_[A-Za-z0-9_]{20,500}"),
    ("Stripe secret key", rb"(?:sk|rk)_(?:test|live)_[A-Za-z0-9_]{8,255}"),
]

PREFIX_REGEX_PATTERNS = {
    "ghp": ("GitHub ghp token", rb"ghp_[A-Za-z0-9_]{20,255}"),
    "gho": ("GitHub gho token", rb"gho_[A-Za-z0-9_]{20,255}"),
    "ghu": ("GitHub ghu token", rb"ghu_[A-Za-z0-9_]{20,255}"),
    "ghs": ("GitHub ghs token", rb"ghs_[A-Za-z0-9_]{20,255}"),
    "ghr": ("GitHub ghr token", rb"ghr_[A-Za-z0-9_]{20,255}"),
    "github_pat": ("GitHub fine-grained token", rb"github_pat_[A-Za-z0-9_]{20,500}"),
    "sk_test": ("Stripe test secret key", rb"sk_test_[A-Za-z0-9_]{8,255}"),
    "sk_live": ("Stripe live secret key", rb"sk_live_[A-Za-z0-9_]{8,255}"),
    "rk_test": ("Stripe test restricted key", rb"rk_test_[A-Za-z0-9_]{8,255}"),
    "rk_live": ("Stripe live restricted key", rb"rk_live_[A-Za-z0-9_]{8,255}"),
}


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


def run_bytes_input(
    args: list[str],
    cwd: Path,
    input_data: bytes,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        args,
        cwd=cwd,
        input=input_data,
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
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        print()
        value = ""
    if value:
        return value
    if default is not None:
        return default
    return ""


def ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{prompt} [{default_text}]: ").strip().lower()
        except EOFError:
            print()
            return default
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def repo_root(path: Path) -> Path:
    if not path.exists():
        raise SystemExit(f"Repository path does not exist: {path}")
    if not path.is_dir():
        raise SystemExit(f"Repository path is not a directory: {path}")
    result = run_quiet(
        ["git", "rev-parse", "--show-toplevel"],
        path,
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = f"Repository path is not inside a Git repository: {path}"
        if detail:
            message += f"\n{detail}"
        raise SystemExit(message)
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


def remote_names(repo: Path) -> list[str]:
    result = run_quiet(["git", "remote"], repo, check=False, capture=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def choose_remote_name(repo: Path) -> str | None:
    remotes = remote_names(repo)
    if not remotes:
        return None
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    print(f"Available remotes: {', '.join(remotes)}")
    while True:
        remote_name = ask("Remote name")
        if not remote_name:
            return None
        if remote_name in remotes:
            return remote_name
        print(f"Remote {remote_name!r} not found.")


def ensure_clean_worktree(repo: Path) -> None:
    result = run_quiet(["git", "status", "--porcelain"], repo, capture=True)
    if result.stdout.strip():
        print(result.stdout)
        if not ask_yes_no("Worktree is dirty. Continue anyway?", default=False):
            raise SystemExit("Aborted because the worktree is dirty.")


def detect_filter_repo(repo: Path) -> FilterRepoCommand:
    result = run_quiet(["git", "filter-repo", "--version"], repo, check=False, capture=True)
    if result.returncode == 0:
        return FilterRepoCommand(args=["git", "filter-repo"], display_name="git filter-repo")
    result = run_quiet(["git-filter-repo", "--version"], repo, check=False, capture=True)
    if result.returncode == 0:
        return FilterRepoCommand(args=["git-filter-repo"], display_name="git-filter-repo")
    raise SystemExit(
        "git-filter-repo was not found.\n"
        "Install it first and restart your shell.\n"
        "Windows PowerShell:\n"
        "  py -m pip install --user pipx\n"
        "  py -m pipx ensurepath\n"
        "  pipx install git-filter-repo\n"
        "macOS/Linux:\n"
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
        for _, pattern in raw_prefix_regex_patterns(patterns):
            temp.write(f"regex:{pattern.decode('ascii')}\n")
        for pattern in patterns:
            if is_known_token_prefix(pattern):
                continue
            temp.write(f"{pattern}\n")
        if include_built_ins:
            for _, pattern in BUILT_IN_REGEX_PATTERNS:
                temp.write(f"regex:{pattern.decode('ascii')}\n")
    return Path(temp.name)


def build_filter_repo_command(
    filter_repo: FilterRepoCommand,
    replacement_file: Path | None,
    paths: list[str],
) -> list[str]:
    command = [*filter_repo.args, "--force"]
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
    regex_patterns = prefix_regex_patterns(patterns)
    if include_built_ins:
        regex_patterns.extend(built_in_regex_patterns())
    found = False
    if scan_commit_messages(repo, exact_patterns, regex_patterns):
        found = True
    objects = reachable_blob_paths(repo)
    if not objects:
        return not found
    print(f"Scanning {len(objects)} reachable blobs...", flush=True)

    def scan_blob(blob: str, paths: list[str], data: bytes) -> None:
        nonlocal found
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

    if not for_each_blob(repo, objects, scan_blob):
        return False
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


def prefix_regex_patterns(patterns: list[str]) -> list[tuple[str, re.Pattern[bytes]]]:
    return [(label, re.compile(regex)) for label, regex in raw_prefix_regex_patterns(patterns)]


def raw_prefix_regex_patterns(patterns: list[str]) -> list[tuple[str, bytes]]:
    expanded: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for pattern in patterns:
        key = pattern.rstrip("_").lower()
        if key not in PREFIX_REGEX_PATTERNS:
            continue
        label, regex = PREFIX_REGEX_PATTERNS[key]
        if regex in seen:
            continue
        seen.add(regex)
        expanded.append((label, regex))
    return expanded


def is_known_token_prefix(pattern: str) -> bool:
    return pattern.rstrip("_").lower() in PREFIX_REGEX_PATTERNS


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
    result = run_quiet(
        ["git", "rev-list", "--objects", "--all"],
        repo,
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        message = f"Unable to list Git objects for repository: {repo}"
        if detail:
            message += f"\n{detail}"
        raise RuntimeError(message)
    if not result.stdout.strip():
        return {}
    object_ids: list[str] = []
    object_paths: dict[str, list[str]] = defaultdict(list)
    for line in result.stdout.splitlines():
        if not line:
            continue
        oid, _, path = line.partition(" ")
        object_ids.append(oid)
        if path:
            object_paths[oid].append(path)

    check_input = ("\n".join(object_ids) + "\n").encode("ascii")
    check = run_bytes_input(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        repo,
        check_input,
        check=False,
    )
    if check.returncode != 0:
        detail = check.stderr.decode("utf-8", errors="replace").strip()
        message = "Unable to inspect Git object types."
        if detail:
            message += f"\n{detail}"
        raise RuntimeError(message)

    blob_paths: dict[str, list[str]] = defaultdict(list)
    for line in check.stdout.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        oid, _, kind = line.partition(" ")
        if kind == "blob":
            blob_paths[oid].extend(object_paths.get(oid, []))
    return dict(blob_paths)


def for_each_blob(
    repo: Path,
    objects: dict[str, list[str]],
    callback: Callable[[str, list[str], bytes], None],
) -> bool:
    if not objects:
        return True
    batch_input = ("\n".join(objects) + "\n").encode("ascii")
    result = run_bytes_input(
        ["git", "cat-file", "--batch"],
        repo,
        batch_input,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", errors="replace"), end="", file=sys.stderr)
        return False

    data = result.stdout
    index = 0
    total = len(data)
    while index < total:
        header_end = data.find(b"\n", index)
        if header_end == -1:
            print("Unexpected git cat-file batch output.", file=sys.stderr)
            return False
        header = data[index:header_end].decode("ascii", errors="replace")
        parts = header.split()
        if len(parts) < 3:
            print(f"Unexpected git cat-file header: {header}", file=sys.stderr)
            return False
        oid, kind, size_text = parts[0], parts[1], parts[2]
        try:
            size = int(size_text)
        except ValueError:
            print(f"Unexpected git cat-file size: {header}", file=sys.stderr)
            return False
        start = header_end + 1
        end = start + size
        if end > total:
            print("Unexpected truncated git cat-file blob output.", file=sys.stderr)
            return False
        if kind == "blob":
            callback(oid, objects.get(oid, []), data[start:end])
        index = end + 1
    return True


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
        raise SystemExit(
            "Git was not found on PATH.\n"
            "Install Git for Windows, then restart PowerShell:\n"
            "  winget install --id Git.Git -e --source winget"
        )
    warn_if_not_git_for_windows()

    repo_input = ask("Repository path", os.getcwd())
    repo = repo_root(Path(repo_input).expanduser().resolve())
    branch = current_branch(repo)
    print(f"Repository: {repo}")

    ensure_clean_worktree(repo)
    filter_repo = detect_filter_repo(repo)

    remote_name = choose_remote_name(repo)
    url = remote_url(repo, remote_name) if remote_name else None
    remote = None
    if remote_name and url:
        remote = RemoteState(
            name=remote_name,
            url=url,
            branch=branch,
            sha=remote_branch_sha(repo, remote_name, branch),
        )
        print(f"Remote: {remote.name}")
    else:
        print("No remote found; push step will be skipped.")

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
        command = build_filter_repo_command(filter_repo, replacement_file, paths)
        print(f"Running {filter_repo.display_name} cleanup...", flush=True)
        run_quiet(command, repo)
        if remote:
            restore_remote(repo, remote)
            print(f"Remote restored: {remote.name}")
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


def warn_if_not_git_for_windows() -> None:
    result = subprocess.run(
        ["git", "--version", "--build-options"],
        text=True,
        capture_output=True,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}".lower()
    if os.name == "nt" and "mingw" not in output and "windows" not in output:
        print(
            "Warning: git was found, but it does not look like Git for Windows.",
            file=sys.stderr,
        )


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
