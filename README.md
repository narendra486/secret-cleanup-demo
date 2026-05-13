# git-filter-repo Secret Cleanup Demo

Official repository:

https://github.com/newren/git-filter-repo

This repository demonstrates removing committed secrets from Git history.

The first commit intentionally contains fake secrets:

- `.env`
- `FAKE_API_TOKEN=<removed from working tree>`

The history is later cleaned with `git filter-repo`.

## Automatic Script

Run the interactive cleanup script:

```bash
python3 git-filter.py
```

PowerShell:

```powershell
py -3 git-filter.py
```

The script is compatible with Python 3.14 and works on Windows because it calls
Git with Python argument lists instead of shell-specific Bash syntax.

Windows prerequisites:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install git-filter-repo
git filter-repo --version
```
