#!/usr/bin/env python3
"""
Collects your commit / line-change stats across every repo you have
write access to (owned, collaborator, or org member), broken down by
language and by category (frontend/backend/UI/infra/etc.).

Env vars:
  STATS_PAT       - GitHub PAT (classic) with `repo` + `read:org` scopes. Required.
  GH_USERNAME     - Your GitHub login. Required.
  DAYS_LOOKBACK   - On first run, how far back to look. Default 730 (two years).
  INCLUDE_ORGS    - Comma-separated org logins to restrict to (optional, default: all).
  EXCLUDE_REPOS   - Comma-separated "owner/repo" to skip (optional). Merged with the
                    in-script EXCLUDE_REPOS_DEFAULT set below.

State is kept in data/state.json so re-runs are incremental (only new
commits since the last processed SHA per repo are fetched). Repos that
are discovered for the first time (e.g. a newly SSO-authorized org) get a
full DAYS_LOOKBACK backfill instead of only "since last run".
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

import hashlib

from zoneinfo import ZoneInfo

from dev_tags import area_for, repo_default_area, is_test, commit_type
from render import render_all, load_profile

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
STATS_FILE = DATA_DIR / "stats.json"
CONFIG_FILE = ROOT / "config" / "categories.yaml"

GITHUB_API = "https://api.github.com"
GRAPHQL_API = "https://api.github.com/graphql"

# Bump when stats.json gains fields that need a full recompute.
SCHEMA_VERSION = 2
# Max lines a single file in a single commit adds to the work-area split.
AREA_FILE_CAP = int(os.environ.get("AREA_FILE_CAP", "800"))
# Commits kept per repo for the "Recently" feed.
RECENT_PER_REPO = 15
# Extra commit-author emails, filled from profile.yaml at runtime.
COMMIT_EMAILS = []

TOKEN = os.environ.get("STATS_PAT")
USERNAME = os.environ.get("GH_USERNAME")
DAYS_LOOKBACK = int(os.environ.get("DAYS_LOOKBACK", "730"))
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

EXT_LANG = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".go": "Go", ".rb": "Ruby", ".php": "PHP", ".java": "Java",
    ".kt": "Kotlin", ".kts": "Kotlin", ".rs": "Rust", ".cs": "C#", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++",
    ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Shell",
    ".ps1": "PowerShell", ".sql": "SQL", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".sass": "Sass", ".less": "Less", ".vue": "Vue", ".svelte": "Svelte",
    ".yml": "YAML", ".yaml": "YAML", ".json": "JSON", ".jsonl": "JSON Lines",
    ".ndjson": "JSON Lines", ".md": "Markdown", ".mdx": "Markdown", ".rst": "reStructuredText",
    ".tf": "Terraform", ".tfvars": "Terraform", ".hcl": "HCL", ".toml": "TOML",
    ".ini": "INI", ".cfg": "INI", ".env": "Dotenv", ".ipynb": "Jupyter Notebook",
    ".r": "R", ".rmd": "R", ".lua": "Lua", ".ex": "Elixir", ".exs": "Elixir",
    ".erl": "Erlang", ".dart": "Dart", ".graphql": "GraphQL", ".gql": "GraphQL",
    ".sol": "Solidity", ".vy": "Vyper", ".proto": "Protocol Buffers", ".txt": "Text",
    ".csv": "CSV", ".tsv": "CSV", ".xml": "XML", ".gradle": "Gradle", ".groovy": "Groovy",
    ".clj": "Clojure", ".hs": "Haskell", ".pl": "Perl", ".pm": "Perl", ".jl": "Julia",
    ".nim": "Nim", ".zig": "Zig", ".png": "Image", ".jpg": "Image", ".jpeg": "Image",
    ".gif": "Image", ".svg": "Image", ".webp": "Image", ".ico": "Image", ".pdf": "PDF",
}

# Files that have no extension (or a misleading one) but are well-known by name.
FILENAME_LANG = {
    "dockerfile": "Docker",
    "makefile": "Makefile",
    "gnumakefile": "Makefile",
    "cmakelists.txt": "CMake",
    "gemfile": "Ruby",
    "rakefile": "Ruby",
    "procfile": "Config",
    "requirements.txt": "Python",
    "pipfile": "Python",
    "go.mod": "Go",
    "go.sum": "Go",
    "cargo.toml": "TOML",
    "package.json": "JSON",
    "tsconfig.json": "JSON",
    ".gitignore": "Config",
    ".gitattributes": "Config",
    ".env": "Dotenv",
}


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


def list_commit_shas(owner, repo, branch, since_iso):
    """List SHAs authored by you on `branch` since `since_iso`: by GitHub login,
    plus any extra `commit_emails` from profile.yaml (commits made with an email
    that isn't linked to your account). Returns (shas, error)."""
    seen, out, err = set(), [], None
    for author in [USERNAME] + COMMIT_EMAILS:
        shas, e = _list_commit_shas(owner, repo, branch, since_iso, author)
        err = err or e
        for sha in shas:
            if sha not in seen:
                seen.add(sha)
                out.append(sha)
    return out, err


def _list_commit_shas(owner, repo, branch, since_iso, author):
    """List SHAs by one author (login or email) on `branch` since `since_iso`.

    Returns (shas, error). On any fetch problem we print a debuggable message
    and return whatever we have plus a short error string (None on success).
    """
    full = f"{owner}/{repo}"
    shas, page = [], 1
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
        shas.extend(c["sha"] for c in batch)
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


def classify_file(filename, config):
    """Return (language, category) for a changed file path.

    Language detection order:
      1. Well-known bare filenames (Dockerfile, Makefile, go.mod, ...).
      2. File extension lookup.
      3. Fallback: a prettified version of the extension, or "Other".
    """
    base = Path(filename).name.lower()
    ext = Path(filename).suffix.lower()

    if base in FILENAME_LANG:
        lang = FILENAME_LANG[base]
    elif ext in EXT_LANG:
        lang = EXT_LANG[ext]
    elif ext:
        # Unknown extension: title-case it instead of shouting in ALL CAPS.
        lang = ext.lstrip(".").capitalize()
    else:
        lang = "Other"

    category = "other"
    for cat, patterns in config["categories"].items():
        if any(re.search(p, filename, re.IGNORECASE) for p in patterns):
            category = cat
            break
    return lang, category


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main():
    global USERNAME
    if not TOKEN or not USERNAME:
        sys.exit("STATS_PAT and GH_USERNAME must be set")

    # Resolve the ACTUAL login that owns the token and use it for the commit
    # `author` filter. Repo discovery uses the token's own identity, so if the
    # GH_USERNAME secret doesn't exactly match your commit-author login, every
    # repo is found but zero commits match — the "stats still zero" bug.
    try:
        token_login = graphql("query { viewer { login } }")["viewer"]["login"]
        if token_login and token_login.lower() != USERNAME.lower():
            print(f"NOTE: GH_USERNAME '{USERNAME}' != token login '{token_login}'. "
                  f"Counting commits by '{token_login}'.")
        USERNAME = token_login or USERNAME
    except Exception as e:  # noqa: BLE001 - fall back to GH_USERNAME
        print(f"Could not resolve token login ({e}); using GH_USERNAME '{USERNAME}'.")

    DATA_DIR.mkdir(exist_ok=True)
    config = yaml.safe_load(CONFIG_FILE.read_text())
    state = load_json(STATE_FILE, {"processed_shas": {}, "last_run": None})
    stats = load_json(STATS_FILE, {
        "totals": {"commits": 0, "additions": 0, "deletions": 0},
        "by_repo": {}, "by_language": {}, "by_category": {}, "by_week": {},
        "by_repo_language": {}, "by_area": {}, "by_repo_area": {}, "by_repo_week": {},
        "schema_version": SCHEMA_VERSION,
    })
    # Ensure key exists when loading older stats files.
    stats.setdefault("by_repo_language", {})

    # Schema bump (v2 adds exact per-repo work areas + per-repo weeks, which can't
    # be derived from older aggregates) -> recompute everything once.
    profile = load_profile()
    repo_areas = profile.get("repo_areas") or {}
    global COMMIT_EMAILS
    COMMIT_EMAILS = [e for e in (profile.get("commit_emails") or []) if e]
    tz = ZoneInfo(profile.get("timezone") or "UTC")
    # Fingerprint of everything that affects classification. Editing the rules in
    # dev_tags.py or `repo_areas` in profile.yaml triggers one full recompute.
    fingerprint = hashlib.sha256(json.dumps({
        "schema": SCHEMA_VERSION,
        "rules": (Path(__file__).parent / "dev_tags.py").read_text(),
        "repo_areas": repo_areas,
        "cap": AREA_FILE_CAP,
        "recent_per_repo": RECENT_PER_REPO,
        "emails": COMMIT_EMAILS,
        "timezone": str(tz),
        "lookback": DAYS_LOOKBACK,
    }, sort_keys=True).encode()).hexdigest()[:16]
    needs_reset = stats.get("classifier") != fingerprint
    if needs_reset:
        print(f"Classifier changed ({stats.get('classifier')} -> {fingerprint}): "
              "full recompute this run.")

    # If a now-excluded repo already contributed to the accumulated aggregates,
    # we can't cleanly subtract it from every breakdown (by_category / by_week
    # aren't stored per-repo), so recompute everything from scratch this once.
    polluted = sorted(r for r in EXCLUDE_REPOS if r in stats.get("by_repo", {}))
    if polluted or needs_reset:
        if polluted:
            print(f"Excluded repo(s) already present in stats: {', '.join(polluted)}.")
        print("Resetting state + stats to recompute clean totals (full backfill this run).")
        state = {"processed_shas": {}, "last_run": None}
        stats = {
            "totals": {"commits": 0, "additions": 0, "deletions": 0},
            "by_repo": {}, "by_language": {}, "by_category": {}, "by_week": {},
            "by_repo_language": {}, "by_area": {}, "by_repo_area": {}, "by_repo_week": {},
            "recent": [], "by_repo_habits": {},
        }
    stats["schema_version"] = SCHEMA_VERSION
    stats["classifier"] = fingerprint
    stats.setdefault("recent", [])
    stats.setdefault("by_repo_habits", {})

    processed = set()
    for repo_shas in state["processed_shas"].values():
        processed.update(repo_shas)

    default_since = (datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)).isoformat()

    print(f"Discovering repos for {USERNAME}...")
    if EXCLUDE_REPOS:
        print(f"Excluding repos: {', '.join(sorted(EXCLUDE_REPOS))}")
    repos = list_accessible_repos()
    # Public metadata for the profile page (descriptions, visibility, stars).
    stats["repo_meta"] = {
        r["nameWithOwner"]: {
            "description": r.get("description") or "",
            "private": bool(r.get("isPrivate")),
            "stars": r.get("stargazerCount", 0),
        } for r in repos
    }
    print(f"Found {len(repos)} writable repos (looking back {DAYS_LOOKBACK} days).")

    fetch_errors = []  # (repo, reason) for repos we couldn't fully collect

    for repo in repos:
        full = repo["nameWithOwner"]
        owner, name = full.split("/")
        branch = repo["defaultBranchRef"]["name"]
        # New repos (never processed before) need a FULL look-back; already-seen
        # repos only need commits since the last successful run. Using the global
        # last_run for everything meant freshly-discovered repos — e.g. a newly
        # SSO-authorized org like `altconvey` — were never backfilled and so
        # showed zero stats even though they were detected.
        first_seen = full not in state["processed_shas"]
        since_iso = default_since if first_seen else (state.get("last_run") or default_since)

        try:
            shas, err = list_commit_shas(owner, name, branch, since_iso)
        except Exception as e:  # noqa: BLE001 - never let one repo kill the run
            print(f"    ! {full}: unexpected error listing commits: {e}")
            fetch_errors.append((full, f"list commits: {e}"))
            continue
        if err:
            fetch_errors.append((full, err))

        new_shas = [s for s in shas if s not in processed]
        if not new_shas:
            continue
        print(f"  {full}: {len(new_shas)} new commit(s)"
              + ("  [backfill]" if first_seen else ""))

        state["processed_shas"].setdefault(full, [])
        repo_area = repo_default_area(full, repo_areas)

        for sha in new_shas:
            try:
                detail = get_commit_detail(owner, name, sha)
            except Exception as e:  # noqa: BLE001
                print(f"    ! {full}@{sha[:7]}: unexpected error: {e}")
                fetch_errors.append((full, f"commit {sha[:7]}: {e}"))
                continue
            if not detail:
                fetch_errors.append((full, f"commit {sha[:7]}: detail unavailable"))
                continue
            # Mark as processed regardless, so we never re-fetch this SHA.
            state["processed_shas"][full].append(sha)
            processed.add(sha)

            # Count only YOUR commits. The REST `author` filter can be loose for
            # commits whose email isn't linked, so double-check the resolved
            # GitHub login and skip anything that clearly isn't you.
            author = detail.get("author") or {}
            if author.get("login") and author["login"].lower() != USERNAME.lower():
                continue
            # Skip merge commits: their diff re-counts changes already attributed
            # to the merged commits, massively inflating "lines I changed".
            if len(detail.get("parents", [])) > 1:
                continue

            # Local time (profile timezone) for days, weeks and the time-of-day heatmap.
            when = datetime.fromisoformat(
                detail["commit"]["author"]["date"].replace("Z", "+00:00")).astimezone(tz)
            commit_date = when.date().isoformat()
            week = (when.date() - timedelta(days=when.weekday())).isoformat()

            stats["totals"]["commits"] += 1
            rstat = stats["by_repo"].setdefault(full, {"commits": 0, "additions": 0, "deletions": 0})
            rstat["commits"] += 1
            wstat = stats["by_week"].setdefault(week, {"commits": 0, "additions": 0, "deletions": 0})
            wstat["commits"] += 1
            rweeks = stats["by_repo_week"].setdefault(full, {})
            rweeks[week] = rweeks.get(week, 0) + 1
            recent = {
                "repo": full, "sha": sha[:7],
                "date": detail["commit"]["author"]["date"],
                "message": (detail["commit"].get("message") or "").split("\n")[0][:120],
                "areas": {},
            }
            habit = stats["by_repo_habits"].setdefault(full, {
                "tests": {"test": 0, "code": 0}, "types": {}, "sizes": [],
                "days": {}, "slots": {}, "lang_months": {},
            })
            month = commit_date[:7]
            c_test = c_code = c_size = 0
            docs_only = True

            for f in detail.get("files", []):
                add, dele = f.get("additions", 0), f.get("deletions", 0)
                lang, cat = classify_file(f["filename"], config)

                stats["totals"]["additions"] += add
                stats["totals"]["deletions"] += dele
                rstat["additions"] += add
                rstat["deletions"] += dele
                wstat["additions"] += add
                wstat["deletions"] += dele

                lstat = stats["by_language"].setdefault(lang, {"additions": 0, "deletions": 0, "files": 0})
                lstat["additions"] += add
                lstat["deletions"] += dele
                lstat["files"] += 1

                # Language-level detail broken down per repository.
                rl = stats["by_repo_language"].setdefault(full, {})
                rlstat = rl.setdefault(lang, {"additions": 0, "deletions": 0, "files": 0})
                rlstat["additions"] += add
                rlstat["deletions"] += dele
                rlstat["files"] += 1

                cstat = stats["by_category"].setdefault(cat, {"additions": 0, "deletions": 0, "files": 0})
                cstat["additions"] += add
                cstat["deletions"] += dele
                cstat["files"] += 1

                # Work area (ai/backend/devops/frontend/other), overall + per repo.
                # Cap each file's contribution to the work-split so generated dumps
                # (notebook outputs, fixtures, vendored code) can't swamp real work.
                area = area_for(f["filename"], lang, repo_area)
                scale = min(1.0, AREA_FILE_CAP / max(add + dele, 1))
                c_add, c_del = round(add * scale), round(dele * scale)
                recent["areas"][area] = recent["areas"].get(area, 0) + c_add + c_del
                if area != "other":
                    docs_only = False
                    c_size += c_add + c_del
                    if is_test(f["filename"]):
                        c_test += c_add + c_del
                    else:
                        c_code += c_add + c_del
                    lm = habit["lang_months"].setdefault(lang, {})
                    lm[month] = lm.get(month, 0) + c_add + c_del
                elif not re.search(r"\.(md|mdx|rst|txt)$|(^|/)docs?/", f["filename"], re.I):
                    docs_only = False
                for bucket in (stats["by_area"], stats["by_repo_area"].setdefault(full, {})):
                    astat = bucket.setdefault(area, {"additions": 0, "deletions": 0, "files": 0})
                    astat["additions"] += c_add
                    astat["deletions"] += c_del
                    astat["files"] += 1

            ctype = commit_type(recent["message"], c_test, c_code, docs_only and bool(detail.get("files")))
            recent["type"] = ctype
            habit["types"][ctype] = habit["types"].get(ctype, 0) + 1
            habit["tests"]["test"] += c_test
            habit["tests"]["code"] += c_code
            habit["sizes"].append(c_size)
            habit["days"][commit_date] = habit["days"].get(commit_date, 0) + 1
            slot = f"{when.weekday()}-{when.hour}"
            habit["slots"][slot] = habit["slots"].get(slot, 0) + 1
            stats["recent"].append(recent)

    # Keep the newest commits *per repo* for the activity feed, so filtering out
    # industry repos on the profile page still leaves plenty of personal history.
    recent, per_repo = [], {}
    for c in sorted(stats["recent"], key=lambda c: c["date"], reverse=True):
        per_repo[c["repo"]] = per_repo.get(c["repo"], 0) + 1
        if per_repo[c["repo"]] <= RECENT_PER_REPO:
            recent.append(c)
    stats["recent"] = recent

    # Pull-request counts (a handful of search calls; refreshed every run).
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)).date().isoformat()
        stats["pull_requests"] = collect_pull_requests(list(profile["professional_orgs"]), since)
    except Exception as e:  # noqa: BLE001 - optional metric
        print(f"  ! pull request counts failed: {e}")

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    stats["generated_at"] = state["last_run"]

    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATS_FILE.write_text(json.dumps(stats, indent=2))
    render_all(stats)

    if fetch_errors:
        print(f"\n{len(fetch_errors)} fetch issue(s) to debug:")
        for full, reason in fetch_errors:
            print(f"  - {full}: {reason}")
        print("Tip: 403 usually means the PAT isn't SSO-authorized for that org, "
              "or is missing the `repo`/`read:org` scopes.")
    print("Done.")


if __name__ == "__main__":
    main()
