#!/usr/bin/env python3
"""Windows launcher for git-filter.py.

This file is intentionally small. The cleanup engine lives in git-filter.py;
this launcher verifies the expected Windows tools are available and then runs
the shared implementation.

Run from PowerShell:
  py -3 git-filter-windows.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path


def load_core():
    core_path = Path(__file__).with_name("git-filter.py")
    spec = importlib.util.spec_from_file_location("git_filter_core", core_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Unable to load cleanup engine: {core_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require_git_for_windows() -> None:
    git = shutil.which("git")
    if git is None:
        raise SystemExit(
            "Git was not found on PATH.\n"
            "Install Git for Windows, then restart PowerShell:\n"
            "  winget install --id Git.Git -e --source winget"
        )

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


def main() -> int:
    require_git_for_windows()
    core = load_core()
    return core.main()


if __name__ == "__main__":
    raise SystemExit(main())
