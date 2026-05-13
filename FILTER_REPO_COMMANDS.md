# Commands Used

Official repository:

https://github.com/newren/git-filter-repo

Install used for this local demo:

```bash
python3 -m venv /tmp/gfr-venv
/tmp/gfr-venv/bin/python -m pip install git-filter-repo
```

Create demo repository:

```bash
git init secret-cleanup-demo
git config user.name "Demo User"
git config user.email "demo@example.invalid"
```

Verify fake secrets existed in history before cleanup.
Use your own secret search patterns here:

```bash
git grep -n '<secret-pattern-1>\|<secret-pattern-2>' $(git rev-list --all)
```

Remove `.env` from all commits and replace matching secret text:

```bash
PATH=/tmp/gfr-venv/bin:$PATH git filter-repo \
  --force \
  --path .env \
  --invert-paths \
  --replace-text ../git-filter-repo-replacements.txt
```

Verify fake secrets are gone from all reachable commits.
Use the same patterns you searched for before cleanup:

```bash
git grep -n '<secret-pattern-1>\|<secret-pattern-2>' $(git rev-list --all)
```

Expected result: no output.
