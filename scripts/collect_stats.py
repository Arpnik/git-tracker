#!/usr/bin/env python3
"""
Fetches your commits from GitHub, adds them to data/stats.json and renders the
profile (render.py).

Incremental, using one small file, data/last_commit.json:
    {"owner/repo": {"time": <committer date of the newest fetched commit>,
                    "recent": [SHAs already counted from the last OVERLAP_DAYS]}}

  * no file (first run): fetch every commit from DAYS_LOOKBACK ago until now and
    build stats.json from scratch;
  * a repo with a stored time: fetch only commits after it (minus OVERLAP_DAYS,
    skipping the SHAs in "recent") and ADD them to the existing stats.json. Nothing already counted is ever removed, so repos you
    lose access to keep their numbers.

Delete data/last_commit.json to force a full re-fetch (e.g. after changing the
classification rules). Repos that can no longer be fetched are carried over.

Env vars:
  STATS_PAT       - GitHub PAT (classic) with `repo` + `read:org` scopes. Required.
  GH_USERNAME     - Your GitHub login. Required.
  DAYS_LOOKBACK   - How far back the first fetch goes. Default 1095 (3 years).
  OVERLAP_DAYS    - Days re-checked before the stored time. Default 90.
  INCLUDE_ORGS    - Comma-separated org logins to restrict to (optional).
  EXCLUDE_REPOS   - Comma-separated "owner/repo" to hide.
"""
import os
import re
import sys
import json
import time
import yaml
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aggregate
from render import render_all, load_profile

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CURSOR_FILE = DATA_DIR / "last_commit.json"
# GitHub filters commits by the time they were made, not when they reached the
# default branch. A commit made on a feature branch and merged days later can be
# older than the stored time, so each run re-checks this many days before it.
# Commits already counted in that window are skipped by SHA.
OVERLAP_DAYS = int(os.environ.get("OVERLAP_DAYS", "90"))
STATS_FILE = DATA_DIR / "stats.json"

GITHUB_API = "https://api.github.com"
GRAPHQL_API = "https://api.github.com/graphql"

# Extra commit-author emails, filled from profile.yaml at runtime.
COMMIT_EMAILS = []

TOKEN = os.environ.get("STATS_PAT")
USERNAME = os.environ.get("GH_USERNAME")
DAYS_LOOKBACK = int(os.environ.get("DAYS_LOOKBACK", "1095"))
INCLUDE_ORGS = {o.strip() for o in os.environ.get("INCLUDE_ORGS", "").split(",") if o.strip()}

# --- Repos to exclude from the analysis -------------------------------------
# Add "owner/repo" entries here to permanently skip a repo (e.g. a repo full of
# generated/vendored files that drowns out your real work). This in-script list
# is merged with anything passed via the EXCLUDE_REPOS env var.
EXCLUDE_REPOS_DEFAULT = {
    "Arpnik/RExploit",
}
EXCLUDE_REPOS = EXCLUDE_REPOS_DEFAULT | {
    r.strip() for r in os.environ.get("EXCLUDE_REPOS", "").split(",") if r.strip()
}

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})

def gh_request(method, url, **kwargs):
    for attempt in range(6):
        r = session.request(method, url, **kwargs)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 30))
            wait = max(reset - time.time(), 5)
            print(f"Rate limited, sleeping {wait:.0f}s...")
            time.sleep(wait)
            continue
        if r.status_code == 202:  # stats being computed, retry
            time.sleep(2)
            continue
        return r
    return r


def graphql(query, variables=None):
    r = gh_request("POST", GRAPHQL_API, json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def _repo_is_usable(n):
    """Shared filtering for a repo node from either discovery path."""
    if not n["defaultBranchRef"]:
        return False
    # Archived repos are read-only but still hold years of history: keep them.
    if not n["isArchived"] and n["viewerPermission"] not in ("WRITE", "ADMIN", "MAINTAIN"):
        return False
    if n["nameWithOwner"] in EXCLUDE_REPOS:
        return False
    if INCLUDE_ORGS and n["owner"]["login"] not in INCLUDE_ORGS:
        return False
    return True


def list_viewer_repos():
    """Repos surfaced directly by viewer.repositories (owner/collaborator/member)."""
    query = """
    query($cursor: String) {
      viewer {
        repositories(first: 100, after: $cursor,
          affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
          pageInfo { hasNextPage endCursor }
          nodes {
            nameWithOwner
            isArchived
            isFork
            isPrivate
            description
            stargazerCount
            viewerPermission
            defaultBranchRef { name }
            owner { login }
          }
        }
      }
    }
    """
    repos, cursor = [], None
    while True:
        data = graphql(query, {"cursor": cursor})
        conn = data["viewer"]["repositories"]
        for n in conn["nodes"]:
            if _repo_is_usable(n):
                repos.append(n)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return repos


def list_viewer_orgs():
    """Every org the authenticated user belongs to."""
    query = """
    query($cursor: String) {
      viewer {
        organizations(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes { login }
        }
      }
    }
    """
    orgs, cursor = [], None
    while True:
        data = graphql(query, {"cursor": cursor})
        conn = data["viewer"]["organizations"]
        orgs.extend(n["login"] for n in conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return orgs


def list_org_repos(org):
    """Explicitly enumerate one org's repositories.

    viewer.repositories() silently omits org repos when a classic PAT has not
    been SSO/SAML-authorized for that org, so we query each org directly as a
    fallback. Returns (repos, error_message).
    """
    query = """
    query($org: String!, $cursor: String) {
      organization(login: $org) {
        repositories(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            nameWithOwner
            isArchived
            isFork
            isPrivate
            description
            stargazerCount
            viewerPermission
            defaultBranchRef { name }
            owner { login }
          }
        }
      }
    }
    """
    repos, cursor = [], None
    while True:
        try:
            data = graphql(query, {"org": org, "cursor": cursor})
        except RuntimeError as e:
            return repos, str(e)
        org_node = data.get("organization")
        if not org_node:
            return repos, "organization returned null (no access / SSO not authorized)"
        conn = org_node["repositories"]
        for n in conn["nodes"]:
            if _repo_is_usable(n):
                repos.append(n)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return repos, None


REPO_FIELDS = """
    nameWithOwner isArchived isFork isPrivate description stargazerCount
    viewerPermission defaultBranchRef { name } owner { login }
"""


def list_contributed_repos(days):
    """Repos you committed to, year by year, from your contribution history.

    Catches what permission-based discovery can't see: open-source repos, old
    collaborations and org repos where you no longer have write access.
    """
    query = """
    query($from: DateTime!, $to: DateTime!) {
      viewer {
        contributionsCollection(from: $from, to: $to) {
          commitContributionsByRepository(maxRepositories: 100) {
            repository { %s }
          }
        }
      }
    }
    """ % REPO_FIELDS
    found = {}
    end = datetime.now(timezone.utc)
    start_limit = end - timedelta(days=days)
    while end > start_limit:
        start = max(end - timedelta(days=365), start_limit)
        try:
            data = graphql(query, {"from": start.isoformat(), "to": end.isoformat()})
            for c in data["viewer"]["contributionsCollection"]["commitContributionsByRepository"]:
                n = c["repository"]
                if n["defaultBranchRef"] and n["nameWithOwner"] not in EXCLUDE_REPOS \
                        and (not INCLUDE_ORGS or n["owner"]["login"] in INCLUDE_ORGS):
                    found[n["nameWithOwner"]] = n
        except Exception as e:  # noqa: BLE001 - discovery is best-effort
            print(f"  ! contribution history {start.date()}..{end.date()}: {e}")
        end = start
    return found


def list_accessible_repos():
    """Merge viewer-level, per-org and contribution-history discovery."""
    by_name = {n["nameWithOwner"]: n for n in list_viewer_repos()}
    viewer_names = set(by_name)

    orgs = list_viewer_orgs()
    if INCLUDE_ORGS:
        orgs = [o for o in orgs if o in INCLUDE_ORGS]

    for org in orgs:
        org_repos, err = list_org_repos(org)
        added = 0
        for n in org_repos:
            if n["nameWithOwner"] not in by_name:
                added += 1
            by_name[n["nameWithOwner"]] = n
        if err:
            print(f"  ! org '{org}': {err}")
            print(f"    -> If you commit there, authorize the PAT for SSO: "
                  f"GitHub → Settings → Developer settings → Personal access tokens "
                  f"→ Configure SSO → Authorize for '{org}'.")
        elif not org_repos:
            print(f"  ! org '{org}': 0 writable repos visible "
                  f"(check membership/permissions or SSO authorization).")
        elif added:
            print(f"  + org '{org}': {added} repo(s) not seen via viewer.repositories.")

    contributed = list_contributed_repos(DAYS_LOOKBACK)
    new_from_history = sorted(set(contributed) - set(by_name))
    for name in new_from_history:
        by_name[name] = contributed[name]
    if new_from_history:
        print(f"Contribution history added {len(new_from_history)} repo(s): "
              f"{', '.join(new_from_history)}")

    # Report anything that only org-discovery found (the altconvey-style gap).
    extra = set(by_name) - viewer_names - set(new_from_history)
    if extra:
        print(f"Recovered {len(extra)} repo(s) missed by viewer.repositories: "
              f"{', '.join(sorted(extra))}")

    return list(by_name.values())


def _list_commits(owner, repo, branch, since_iso, author):
    """{sha: committer_date} by one author (login or email) on `branch` since
    `since_iso` (inclusive).

    Returns (commits, error). On any fetch problem we print a debuggable message
    and return whatever we have plus a short error string (None on success).
    """
    full = f"{owner}/{repo}"
    shas, page = {}, 1
    while True:
        r = gh_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/commits", params={
            "sha": branch, "author": author, "since": since_iso,
            "per_page": 100, "page": page,
        })
        if r.status_code == 409:  # empty repository — nothing to do, not an error
            return shas, None
        if r.status_code == 404:
            msg = f"404 (branch '{branch}' or repo not accessible with this token)"
            print(f"    ! {full}: {msg}")
            return shas, msg
        if r.status_code == 403:
            # Forbidden: SSO not authorized, missing scope, or rate limit exhausted.
            detail = "SSO not authorized / missing scope / rate limited"
            print(f"    ! {full}: 403 fetching commits ({detail}). {r.text[:160]}")
            return shas, f"403 {detail}"
        if r.status_code != 200:
            print(f"    ! {full}: HTTP {r.status_code} fetching commits: {r.text[:160]}")
            return shas, f"HTTP {r.status_code}"
        batch = r.json()
        if not batch:
            break
        shas.update((c["sha"], c["commit"]["committer"]["date"]) for c in batch)
        if len(batch) < 100:
            break
        page += 1
    return shas, None


def search_count(q):
    r = gh_request("GET", f"{GITHUB_API}/search/issues", params={"q": q, "per_page": 1})
    if r.status_code != 200:
        return None
    return r.json().get("total_count")


def collect_pull_requests(orgs, since):
    """Pull requests opened / merged / reviewed: overall and per company org."""
    def counts(extra=""):
        base = f"is:pr created:>={since} {extra}".strip()
        return {
            "opened": search_count(f"{base} author:{USERNAME}"),
            "merged": search_count(f"{base} author:{USERNAME} is:merged"),
            "reviewed": search_count(f"{base} reviewed-by:{USERNAME} -author:{USERNAME}"),
        }
    out = {"since": since, "all": counts(), "orgs": {}}
    for org in orgs:
        out["orgs"][org] = counts(f"org:{org}")
    return out


def get_commit_detail(owner, repo, sha):
    r = gh_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}")
    if r.status_code != 200:
        print(f"    ! {owner}/{repo}@{sha[:7]}: HTTP {r.status_code} fetching commit detail")
        return None
    return r.json()


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_repo(repo, cursor, now):
    """Fetch this repo's commits after its cursor (or the whole look-back).

    Returns (records, new_cursor, error). On any error the records are dropped
    by the caller and the cursor stays put, so the next run simply retries.
    """
    full = repo["nameWithOwner"]
    owner, name = full.split("/")
    branch = repo["defaultBranchRef"]["name"]
    if cursor:
        last = datetime.fromisoformat(cursor["time"].replace("Z", "+00:00"))
        since = _iso(last - timedelta(days=OVERLAP_DAYS))
        counted = set(cursor.get("recent", []))
    else:
        since, counted = _iso(now - timedelta(days=DAYS_LOOKBACK)), set()

    listed = {}
    for author in [USERNAME] + COMMIT_EMAILS:
        got, err = _list_commits(owner, name, branch, since, author)
        if err:
            return [], cursor, err
        listed.update(got)
    if not listed:
        return [], cursor, None

    records = []
    for sha in listed:
        if sha in counted:
            continue
        detail = get_commit_detail(owner, name, sha)
        if not detail:
            return [], cursor, f"commit {sha[:7]}: detail fetch failed"
        login = (detail.get("author") or {}).get("login")
        # Not yours (loose REST author match) or a merge commit (its diff
        # re-counts the merged work).
        if (login and login.lower() != USERNAME.lower()) or len(detail.get("parents", [])) > 1:
            continue
        records.append({
            "sha": sha,
            "date": detail["commit"]["author"]["date"],
            "message": (detail["commit"].get("message") or "").split("\n")[0][:200],
            "files": [[f["filename"], f.get("additions", 0), f.get("deletions", 0)]
                      for f in detail.get("files", [])],
        })
    # Remember every SHA from the new overlap window (including skipped merges),
    # so the next run doesn't count or re-fetch them.
    newest = max(listed.values())
    if cursor and cursor["time"] > newest:
        newest = cursor["time"]
    cutoff = _iso(datetime.fromisoformat(newest.replace("Z", "+00:00")) - timedelta(days=OVERLAP_DAYS))
    recent = sorted(s for s, t in listed.items() if t >= cutoff)
    return records, {"time": newest, "recent": recent}, None


def main():
    global USERNAME, COMMIT_EMAILS
    if not TOKEN or not USERNAME:
        sys.exit("STATS_PAT and GH_USERNAME must be set")

    # Resolve the ACTUAL login that owns the token and use it for the commit
    # `author` filter (GH_USERNAME may differ in case or be stale).
    try:
        token_login = graphql("query { viewer { login } }")["viewer"]["login"]
        if token_login and token_login.lower() != USERNAME.lower():
            print(f"NOTE: GH_USERNAME '{USERNAME}' != token login '{token_login}'. "
                  f"Counting commits by '{token_login}'.")
        USERNAME = token_login or USERNAME
    except Exception as e:  # noqa: BLE001 - fall back to GH_USERNAME
        print(f"Could not resolve token login ({e}); using GH_USERNAME '{USERNAME}'.")

    DATA_DIR.mkdir(exist_ok=True)
    profile = load_profile()
    COMMIT_EMAILS = [e for e in (profile.get("commit_emails") or []) if e]

    old = load_json(STATS_FILE, {})
    first_run = not CURSOR_FILE.exists()
    cursors = load_json(CURSOR_FILE, {})
    now = datetime.now(timezone.utc)

    print(f"Discovering repos for {USERNAME}...")
    if EXCLUDE_REPOS:
        print(f"Excluding repos: {', '.join(sorted(EXCLUDE_REPOS))}")
    repos = [r for r in list_accessible_repos() if r["nameWithOwner"] not in EXCLUDE_REPOS]
    print(f"Found {len(repos)} repos." + ("  First run: fetching the full "
          f"{DAYS_LOOKBACK}-day look-back." if first_run else ""))

    # Repo metadata is kept forever, so a repo whose access is later revoked
    # keeps its last known visibility (private repos stay unlinked).
    repo_meta = dict(old.get("repo_meta", {})) if not first_run else {}
    for r in repos:
        repo_meta[r["nameWithOwner"]] = {
            "description": r.get("description") or "",
            "private": bool(r.get("isPrivate")),
            "stars": r.get("stargazerCount", 0),
        }

    new_commits, fetch_errors = {}, []
    for repo in repos:
        full = repo["nameWithOwner"]
        try:
            records, cursor, err = fetch_repo(repo, cursors.get(full), now)
        except Exception as e:  # noqa: BLE001 - never let one repo kill the run
            records, cursor, err = [], cursors.get(full), str(e)
        if err:
            print(f"    ! {full}: {err} (will retry next run)")
            fetch_errors.append((full, err))
            continue
        if records:
            print(f"  {full}: {len(records)} new commit(s)")
            new_commits[full] = records
        if cursor:
            cursors[full] = cursor
    print(f"Fetched {sum(map(len, new_commits.values()))} new commit(s) this run.")

    if first_run:
        # Build from scratch. Repos in the previous stats.json that couldn't be
        # fetched now (e.g. access revoked) are carried over, and get a cursor
        # so a later fetch only adds what's newer.
        stats = aggregate.build(new_commits, profile, EXCLUDE_REPOS, repo_meta, legacy=old)
        stamp = _iso(datetime.fromisoformat(old["generated_at"])) if old.get("generated_at") else _iso(now)
        for repo in stats["legacy_repos"]:
            cursors.setdefault(repo, {"time": stamp, "recent": []})
            repo_meta.setdefault(repo, old.get("repo_meta", {}).get(repo, {"private": True}))
    else:
        delta = aggregate.build(new_commits, profile, EXCLUDE_REPOS, repo_meta)
        stats = aggregate.merge(old, delta)
    stats.pop("legacy_repos", None)
    stats["repo_meta"] = repo_meta

    # Pull-request counts (a handful of search calls; refreshed every run).
    try:
        since = (now - timedelta(days=DAYS_LOOKBACK)).date().isoformat()
        stats["pull_requests"] = collect_pull_requests(list(profile["professional_orgs"]), since)
    except Exception as e:  # noqa: BLE001 - optional metric; keep last known
        print(f"  ! pull request counts failed: {e}")
        if old.get("pull_requests"):
            stats["pull_requests"] = old["pull_requests"]

    stats["generated_at"] = now.isoformat()
    # Both files are written together at the end and committed together by the
    # workflow, so the cursors always match what stats.json already counts.
    STATS_FILE.write_text(json.dumps(stats, indent=1))
    CURSOR_FILE.write_text(json.dumps(dict(sorted(cursors.items())), indent=1))
    render_all(stats)

    if fetch_errors:
        print(f"\n{len(fetch_errors)} repo(s) failed and will be retried next run:")
        for full, reason in fetch_errors:
            print(f"  - {full}: {reason}")
        print("Tip: 403 usually means the PAT isn't SSO-authorized for that org, "
              "or is missing the `repo`/`read:org` scopes.")
    print("Done.")


if __name__ == "__main__":
    main()
