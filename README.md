# git-filter-repo Secret Cleanup Demo

Official repository:

https://github.com/newren/git-filter-repo

This repository demonstrates removing committed secrets from Git history.

The first commit intentionally contains fake secrets:

- `.env`
- `FAKE_API_TOKEN=***REMOVED***`

The history is later cleaned with `git filter-repo`.
