#!/usr/bin/env python3
"""
Collects your commit / line-change stats across every repo you have
write access to (owned, collaborator, or org member), broken down by
language and by category (frontend/backend/UI/infra/etc.).

Env vars:
  STATS_PAT       - GitHub PAT (classic) with `repo` + `read:org` scopes. Required.
  GH_USERNAME     - Your GitHub login. Required.
  DAYS_LOOKBACK   - On first run, how far back to look. Default 90.
  INCLUDE_ORGS    - Comma-separated org logins to restrict to (optional, default: all).
  EXCLUDE_REPOS   - Comma-separated "owner/repo" to skip (optional).

State is kept in data/state.json so re-runs are incremental (only new
commits since the last processed SHA per repo are fetched).
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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
STATS_FILE = DATA_DIR / "stats.json"
CONFIG_FILE = ROOT / "config" / "categories.yaml"

GITHUB_API = "https://api.github.com"
GRAPHQL_API = "https://api.github.com/graphql"

TOKEN = os.environ.get("STATS_PAT")
USERNAME = os.environ.get("GH_USERNAME")
DAYS_LOOKBACK = int(os.environ.get("DAYS_LOOKBACK", "90"))
INCLUDE_ORGS = {o.strip() for o in os.environ.get("INCLUDE_ORGS", "").split(",") if o.strip()}
EXCLUDE_REPOS = {r.strip() for r in os.environ.get("EXCLUDE_REPOS", "").split(",") if r.strip()}

if not TOKEN or not USERNAME:
    sys.exit("STATS_PAT and GH_USERNAME must be set")

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})

EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".go": "Go", ".rb": "Ruby", ".php": "PHP", ".java": "Java",
    ".kt": "Kotlin", ".rs": "Rust", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++",
    ".hpp": "C++", ".swift": "Swift", ".m": "Objective-C", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Shell", ".sql": "SQL", ".html": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".sass": "Sass", ".less": "Less", ".vue": "Vue", ".svelte": "Svelte",
    ".yml": "YAML", ".yaml": "YAML", ".json": "JSON", ".md": "Markdown", ".tf": "Terraform",
    ".dockerfile": "Docker", ".ipynb": "Jupyter Notebook", ".r": "R", ".lua": "Lua",
    ".ex": "Elixir", ".exs": "Elixir", ".dart": "Dart", ".graphql": "GraphQL",
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


def list_accessible_repos():
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
            if n["isArchived"] or not n["defaultBranchRef"]:
                continue
            if n["viewerPermission"] not in ("WRITE", "ADMIN", "MAINTAIN"):
                continue
            if n["nameWithOwner"] in EXCLUDE_REPOS:
                continue
            if INCLUDE_ORGS and n["owner"]["login"] not in INCLUDE_ORGS:
                continue
            repos.append(n)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return repos


def list_commit_shas(owner, repo, branch, since_iso):
    shas, page = [], 1
    while True:
        r = gh_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/commits", params={
            "sha": branch, "author": USERNAME, "since": since_iso,
            "per_page": 100, "page": page,
        })
        if r.status_code in (409, 404):  # empty repo / no access to branch
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        shas.extend(c["sha"] for c in batch)
        if len(batch) < 100:
            break
        page += 1
    return shas


def get_commit_detail(owner, repo, sha):
    r = gh_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}")
    if r.status_code != 200:
        return None
    return r.json()


def classify_file(filename, config):
    ext = Path(filename).suffix.lower()
    lang = EXT_LANG.get(ext, ext.lstrip(".").upper() or "Other")
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
    DATA_DIR.mkdir(exist_ok=True)
    config = yaml.safe_load(CONFIG_FILE.read_text())
    state = load_json(STATE_FILE, {"processed_shas": {}, "last_run": None})
    stats = load_json(STATS_FILE, {
        "totals": {"commits": 0, "additions": 0, "deletions": 0},
        "by_repo": {}, "by_language": {}, "by_category": {}, "by_week": {},
    })

    processed = set()
    for repo_shas in state["processed_shas"].values():
        processed.update(repo_shas)

    default_since = (datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)).isoformat()

    print(f"Discovering repos for {USERNAME}...")
    repos = list_accessible_repos()
    print(f"Found {len(repos)} writable repos.")

    for repo in repos:
        full = repo["nameWithOwner"]
        owner, name = full.split("/")
        branch = repo["defaultBranchRef"]["name"]
        since_iso = state.get("last_run") or default_since

        shas = list_commit_shas(owner, name, branch, since_iso)
        new_shas = [s for s in shas if s not in processed]
        if not new_shas:
            continue
        print(f"  {full}: {len(new_shas)} new commit(s)")

        state["processed_shas"].setdefault(full, [])

        for sha in new_shas:
            detail = get_commit_detail(owner, name, sha)
            if not detail:
                continue
            state["processed_shas"][full].append(sha)
            processed.add(sha)

            commit_date = detail["commit"]["author"]["date"][:10]
            week = (datetime.fromisoformat(commit_date) - timedelta(
                days=datetime.fromisoformat(commit_date).weekday())).strftime("%Y-%m-%d")

            stats["totals"]["commits"] += 1
            rstat = stats["by_repo"].setdefault(full, {"commits": 0, "additions": 0, "deletions": 0})
            rstat["commits"] += 1
            wstat = stats["by_week"].setdefault(week, {"commits": 0, "additions": 0, "deletions": 0})
            wstat["commits"] += 1

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

                cstat = stats["by_category"].setdefault(cat, {"additions": 0, "deletions": 0, "files": 0})
                cstat["additions"] += add
                cstat["deletions"] += dele
                cstat["files"] += 1

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    stats["generated_at"] = state["last_run"]

    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATS_FILE.write_text(json.dumps(stats, indent=2))
    write_readme(stats)
    print("Done.")


def write_readme(stats):
    lines = ["# Dev Stats\n", f"_Last updated: {stats['generated_at']}_\n"]
    t = stats["totals"]
    lines.append(f"**Totals:** {t['commits']} commits · +{t['additions']} / -{t['deletions']} lines\n")

    lines.append("## By category\n")
    lines.append("| Category | + | - | Files touched |")
    lines.append("|---|---|---|---|")
    for cat, s in sorted(stats["by_category"].items(), key=lambda x: -x[1]["additions"]):
        lines.append(f"| {cat} | {s['additions']} | {s['deletions']} | {s['files']} |")

    lines.append("\n## By language\n")
    lines.append("| Language | + | - | Files touched |")
    lines.append("|---|---|---|---|")
    for lang, s in sorted(stats["by_language"].items(), key=lambda x: -x[1]["additions"])[:20]:
        lines.append(f"| {lang} | {s['additions']} | {s['deletions']} | {s['files']} |")

    lines.append("\n## By repository\n")
    lines.append("| Repo | Commits | + | - |")
    lines.append("|---|---|---|---|")
    for repo, s in sorted(stats["by_repo"].items(), key=lambda x: -x[1]["additions"]):
        lines.append(f"| {repo} | {s['commits']} | {s['additions']} | {s['deletions']} |")

    (ROOT / "README_STATS.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()