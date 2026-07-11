# git-tracker

Tracks your commit activity — lines added/removed, by language, by
category (frontend / backend / UI / infra / etc.) — across **every
repo you have write access to**: your own, other people's repos
you're a collaborator on, and org repos.

## How it works

1. A scheduled GitHub Action (`.github/workflows/track-stats.yml`) runs daily.
2. `scripts/collect_stats.py` uses the GitHub API to:
   - list every repo you can push to (GraphQL, `viewer.repositories`)
   - list your commits on each repo's default branch since the last run (REST `/commits`)
   - pull the per-file diff stats for each new commit (REST `/commits/{sha}`)
   - classify each changed file by language (extension) and category
     (regex patterns in `config/categories.yaml`)
3. Results are written to `data/stats.json` (raw) and `README_STATS.md`
   (human-readable tables), then committed back to this repo.
4. `dashboard/index.html` reads `data/stats.json` and renders charts —
   open it locally, or serve it via GitHub Pages.

## Setup

1. Create this repo (private is recommended, since it lists other repos' names).
2. Create a **classic** Personal Access Token: GitHub → Settings → Developer
   settings → Personal access tokens → Tokens (classic).
   Scopes needed: `repo`, `read:org`.
   (A fine-grained PAT works too, but you'd have to approve it per-org, which
   defeats the "see everything I have access to" goal.)
3. In this repo's Settings → Secrets and variables → Actions, add:
   - `STATS_PAT` — the token from step 2
   - `GH_USERNAME` — your GitHub username
4. Push. The workflow runs on its schedule, or trigger it manually from
   the Actions tab (`workflow_dispatch`).
5. Optional: enable GitHub Pages on the `dashboard/` folder (or a
   `gh-pages` branch you deploy it to) to get a hosted chart view.

## Customizing categories

Edit `config/categories.yaml`. Each category is a list of regexes
tested against the file path; first match wins, so put more specific
categories above catch-alls.

## Notes / limits

- Only counts commits authored by `GH_USERNAME` on each repo's **default
  branch** (extend `list_commit_shas` if you want all branches — costs more API calls).
  - State is incremental (`data/state.json`), so re-runs are cheap.
- First run looks back `DAYS_LOOKBACK` days (default 90) — bump this in
  the workflow env if you want more history, but expect more API calls
  on that first run.
- Rate limits: a PAT gets 5,000 REST calls/hour and a separate GraphQL
  budget, which is generous for this after the first backfill.# git-tracker