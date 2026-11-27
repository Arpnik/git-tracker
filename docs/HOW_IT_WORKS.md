# How git-tracker works

git-tracker turns real commit history into a GitHub profile page. Nothing on it is
hand-written: [`README.md`](../README.md), the SVG charts in [`assets/`](../assets/)
and the [interactive dashboard](../index.html) are regenerated every day by
[`track-stats.yml`](../.github/workflows/track-stats.yml).

```
GitHub API ──► collect_stats.py ──► data/stats.json ──► render.py ──► README · SVGs · dashboard
  (daily)        classify every changed file              (no token needed)     │
                                                                               └─► <user>/<user> profile repo
```

## Pipeline

1. **Discover** every repo you committed to in the look-back window (3 years by default):
   - repos the token can push to (own, collaborations, org repos, with a per-org fallback for SSO);
   - **archived** repos, even if they're now read-only;
   - repos found in your **contribution history**, year by year: open-source projects and old
     collaborations you no longer have write access to.

   Commits are matched by your GitHub login plus any `commit_emails` in `profile.yaml`
   (for commits made with an email that isn't linked to your account).
2. **Collect incrementally**: for each commit authored by you on the default branch
   (merge commits skipped), fetch per-file diff stats. The newest fetched commit's time
   and SHA per repo are saved in `data/last_commit.json`.
   - **First run** (no `last_commit.json`): fetch everything from 3 years ago until now and
     build `data/stats.json` from scratch.
   - **Daily runs**: fetch only commits after each repo's stored time and **add** them to the
     existing `stats.json`. Typically one listing call per repo plus one per new commit.
   - **Late merges**: GitHub filters by when a commit was made, not when it reached the
     default branch, so each run re-checks the 90 days before the stored time
     (`OVERLAP_DAYS`). The SHAs already counted in that window are kept in `last_commit.json`
     and skipped, so nothing is counted twice.
   - A repo that fails keeps its old time and is retried next run.
   - **Revoked access never loses data.** Numbers already in `stats.json` are never removed,
     and repo metadata (e.g. "private") is remembered.
   - `stats.json` never contains commit messages from private repos.
3. **Classify**: each changed file gets a language and one **work area**
   ([`scripts/dev_tags.py`](../scripts/dev_tags.py)), decided in this order:

   | Step | Example |
   |---|---|
   | Noise → *Other* | lockfiles, docs, images, CSV/JSONL data |
   | Strong file signal | `Dockerfile`, `*.tf`, CI → **DevOps** · `.tsx`/`.css`/`.dart` → **Frontend** · `.ipynb`, model files → **AI/ML** · `.sql`, migrations, `.sol` → **Backend** · `openai`/`embedding`/`prompt` in a path → **AI/ML** |
   | Repo discipline | a plain `.js` file in `knowbie-back` → **Backend**; in `acy-ui` → **Frontend** |
   | Folder convention | `routes/`, `controllers/` → Backend · `components/`, `screens/` → Frontend |
   | Language fallback | JS/TS → Frontend, Python/Java/Go → Backend |

   A repo's discipline comes from `repo_areas` in [`config/profile.yaml`](../config/profile.yaml),
   or else from name hints (`-back`, `-api`, `-ui`, `-ai`, …).

   Each file contributes at most **800 lines per commit** to the work split
   (`AREA_FILE_CAP` in `aggregate.py`), so notebook outputs and generated files can't swamp real work.
   Raw line totals elsewhere are uncapped.
4. **Habits**: per commit, the collector also records:
   - test vs non-test lines;
   - a commit type: feature / fix / refactor / test / docs / build, from conventional-commit
     prefixes, what the diff touched, then message keywords;
   - commit size, the local day and hour (`timezone` in `profile.yaml`), and languages per month.

   From these it derives test share, median commit size, commit mix, streaks, a weekday × hour
   heatmap and recent languages. Pull-request counts (opened / merged / reviewed) come from the
   search API on each run.
5. **Scope**: repos owned by `professional_orgs` are **Industry** work, everything else
   **Personal**.
6. **Render**: [`scripts/render.py`](../scripts/render.py) writes the README, the donut and
   activity SVGs, `README_STATS.md` and `index.html`.
7. **Publish**: the workflow copies `README.md` + `assets/` to your profile repo
   (`<user>/<user>`), so it shows on your GitHub profile page.

Because new commits are added on top of existing totals, changes to the rules in `dev_tags.py`,
`repo_areas` or the timezone only apply to commits fetched after the change. To re-apply them to
all history, delete `data/last_commit.json`: the next run re-fetches the 3 years and rebuilds,
carrying over repos that can no longer be fetched.

## Set it up for yourself

1. Fork this repo and keep it **public**.
2. Create a **classic** PAT with `repo` + `read:org`. For orgs with SAML SSO, use
   *Configure SSO → Authorize* on the token.
3. Add repo secrets `STATS_PAT` and `GH_USERNAME`.
4. Create your profile repo: a **public** repo named exactly like your username
   (e.g. `Arpnik/Arpnik`). The workflow pushes the README there. The PAT above already
   has access.
5. Edit `config/profile.yaml`: name, about, links, company orgs, repo disciplines, featured.
6. Run the workflow from **Actions → Track Dev Stats → Run workflow**.
7. **Settings → Pages → Deploy from branch → `main` / root** hosts the dashboard at
   `https://<user>.github.io/git-tracker/`.

Preview locally without a token:

```bash
pip install -r requirements.txt
python scripts/render.py     # then open README.md / index.html
```

## Troubleshooting

- **An org's repos are missing**: almost always SAML SSO. Authorize the classic PAT for that
  org. The Action log prints `! org '<name>': ...` with the fix. Other causes: read-only access,
  missing `read:org`, or `INCLUDE_ORGS` excluding it.
- **Profile repo didn't update**: check the *Publish profile README* step. It skips with a
  warning if `PROFILE_REPO` doesn't exist or the token can't push to it.
- **A repo is classified wrong**: set it in `repo_areas` in `profile.yaml`.
- **Skip a noisy repo**: add it to `EXCLUDE_REPOS_DEFAULT` in `collect_stats.py` or the
  `EXCLUDE_REPOS` env var.
- **Confidentiality**: private industry repos show by name (no link). Their commits are left
  out of the "Recently" feed unless `activity.include_professional` is `true`.
- Rate limits: 5,000 REST calls/hour. Only the first run is heavy (about one call per commit);
  daily runs need roughly one call per repo plus one per new commit.
