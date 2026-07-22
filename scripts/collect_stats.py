#!/usr/bin/env python3
"""
Collects your commit / line-change stats across every repo you have
write access to (owned, collaborator, or org member), broken down by
language and by category (frontend/backend/UI/infra/etc.).

Env vars:
  STATS_PAT       - GitHub PAT (classic) with `repo` + `read:org` scopes. Required.
  GH_USERNAME     - Your GitHub login. Required.
  DAYS_LOOKBACK   - On first run, how far back to look. Default 365 (one year).
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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
STATS_FILE = DATA_DIR / "stats.json"
CONFIG_FILE = ROOT / "config" / "categories.yaml"

GITHUB_API = "https://api.github.com"
GRAPHQL_API = "https://api.github.com/graphql"

TOKEN = os.environ.get("STATS_PAT")
USERNAME = os.environ.get("GH_USERNAME")
DAYS_LOOKBACK = int(os.environ.get("DAYS_LOOKBACK", "365"))
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
    if n["isArchived"] or not n["defaultBranchRef"]:
        return False
    if n["viewerPermission"] not in ("WRITE", "ADMIN", "MAINTAIN"):
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


def list_accessible_repos():
    """Merge viewer-level and per-org discovery, deduped by nameWithOwner."""
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

    # Report anything that only org-discovery found (the altconvey-style gap).
    extra = set(by_name) - viewer_names
    if extra:
        print(f"Recovered {len(extra)} repo(s) missed by viewer.repositories: "
              f"{', '.join(sorted(extra))}")

    return list(by_name.values())


def list_commit_shas(owner, repo, branch, since_iso):
    """List SHAs authored by USERNAME on `branch` since `since_iso`.

    Returns (shas, error). On any fetch problem we print a debuggable message
    and return whatever we have plus a short error string (None on success).
    """
    full = f"{owner}/{repo}"
    shas, page = [], 1
    while True:
        r = gh_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/commits", params={
            "sha": branch, "author": USERNAME, "since": since_iso,
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
    if not TOKEN or not USERNAME:
        sys.exit("STATS_PAT and GH_USERNAME must be set")
    DATA_DIR.mkdir(exist_ok=True)
    config = yaml.safe_load(CONFIG_FILE.read_text())
    state = load_json(STATE_FILE, {"processed_shas": {}, "last_run": None})
    stats = load_json(STATS_FILE, {
        "totals": {"commits": 0, "additions": 0, "deletions": 0},
        "by_repo": {}, "by_language": {}, "by_category": {}, "by_week": {},
        "by_repo_language": {},
    })
    # Ensure key exists when loading older stats files.
    stats.setdefault("by_repo_language", {})

    # If a now-excluded repo already contributed to the accumulated aggregates,
    # we can't cleanly subtract it from every breakdown (by_category / by_week
    # aren't stored per-repo), so recompute everything from scratch this once.
    polluted = sorted(r for r in EXCLUDE_REPOS if r in stats.get("by_repo", {}))
    if polluted:
        print(f"Excluded repo(s) already present in stats: {', '.join(polluted)}.")
        print("Resetting state + stats to recompute clean totals (full backfill this run).")
        state = {"processed_shas": {}, "last_run": None}
        stats = {
            "totals": {"commits": 0, "additions": 0, "deletions": 0},
            "by_repo": {}, "by_language": {}, "by_category": {}, "by_week": {},
            "by_repo_language": {},
        }

    processed = set()
    for repo_shas in state["processed_shas"].values():
        processed.update(repo_shas)

    default_since = (datetime.now(timezone.utc) - timedelta(days=DAYS_LOOKBACK)).isoformat()

    print(f"Discovering repos for {USERNAME}...")
    if EXCLUDE_REPOS:
        print(f"Excluding repos: {', '.join(sorted(EXCLUDE_REPOS))}")
    repos = list_accessible_repos()
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

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    stats["generated_at"] = state["last_run"]

    STATE_FILE.write_text(json.dumps(state, indent=2))
    STATS_FILE.write_text(json.dumps(stats, indent=2))
    write_readme(stats)
    write_html_report(stats)

    if fetch_errors:
        print(f"\n{len(fetch_errors)} fetch issue(s) to debug:")
        for full, reason in fetch_errors:
            print(f"  - {full}: {reason}")
        print("Tip: 403 usually means the PAT isn't SSO-authorized for that org, "
              "or is missing the `repo`/`read:org` scopes.")
    print("Done.")


def write_readme(stats):
    lines = ["# Dev Stats\n", f"_Last updated: {stats['generated_at']}_\n"]
    lines.append("> 📊 Rendered HTML version with charts: "
                 "[`README_STATS.html`](./README_STATS.html)\n")
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

    # Language-level detail per repository.
    lines.append("\n## Language detail by repository\n")
    for repo, langs in sorted(stats.get("by_repo_language", {}).items(),
                              key=lambda x: -sum(v["additions"] for v in x[1].values())):
        lines.append(f"\n<details><summary><strong>{repo}</strong></summary>\n")
        lines.append("| Language | + | - | Files touched |")
        lines.append("|---|---|---|---|")
        for lang, s in sorted(langs.items(), key=lambda x: -x[1]["additions"]):
            lines.append(f"| {lang} | {s['additions']} | {s['deletions']} | {s['files']} |")
        lines.append("\n</details>")

    (ROOT / "README_STATS.md").write_text("\n".join(lines) + "\n")


def write_html_report(stats):
    """Write a self-contained HTML summary (charts + tables incl. language detail).

    The stats payload is embedded inline so the file works when opened directly
    from disk or served via GitHub Pages, with no separate fetch of stats.json.
    """
    payload = json.dumps(stats)
    html = HTML_TEMPLATE.replace("__STATS_JSON__", payload)
    (ROOT / "README_STATS.html").write_text(html)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Dev Stats</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js"></script>
<style>
  :root{--bg:#0d1117;--panel:#11161d;--line:#1f2630;--text:#c9d1d9;--dim:#6e7681;
    --accent:#3fb950;--accent2:#58a6ff;--warn:#e3b341;
    --mono:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px}
  header{padding:28px 32px 16px;border-bottom:1px solid var(--line)}
  header .prompt{color:var(--accent)}
  h1{margin:4px 0 0;font-size:22px;font-weight:600;letter-spacing:-.5px}
  .sub{color:var(--dim);margin-top:6px}
  main{padding:24px 32px;display:grid;gap:20px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:18px}
  .card h2{margin:0 0 14px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);font-weight:600}
  .totals{display:flex;gap:28px;flex-wrap:wrap}
  .stat .n{font-size:28px;font-weight:700}
  .stat .l{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  .add{color:var(--accent)} .del{color:#f85149}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td,th{padding:6px 4px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:500;text-transform:uppercase;font-size:11px}
  .full{grid-column:1/-1}
  canvas{max-height:280px}
  details{margin:8px 0;border:1px solid var(--line);border-radius:6px;padding:8px 12px}
  summary{cursor:pointer;color:var(--accent2)}
</style>
</head>
<body>
<header>
  <div class="prompt">$ dev-stats --report</div>
  <h1 id="ts">loading...</h1>
  <div class="sub">summary of code changes across every repo you have write access to</div>
</header>
<main id="app"></main>
<script>
const stats = __STATS_JSON__;
function el(tag, cls, html){const e=document.createElement(tag);if(cls)e.className=cls;if(html!=null)e.innerHTML=html;return e;}
function render(){
  document.getElementById('ts').textContent = 'updated ' + new Date(stats.generated_at).toLocaleString();
  const app = document.getElementById('app');

  const totals = el('div','card full',
    '<h2>totals</h2><div class="totals">'
    + '<div class="stat"><div class="n">'+stats.totals.commits+'</div><div class="l">commits</div></div>'
    + '<div class="stat"><div class="n add">+'+stats.totals.additions.toLocaleString()+'</div><div class="l">lines added</div></div>'
    + '<div class="stat"><div class="n del">-'+stats.totals.deletions.toLocaleString()+'</div><div class="l">lines removed</div></div>'
    + '</div>');
  app.appendChild(totals);

  app.appendChild(el('div','card','<h2>by category</h2><canvas id="catChart"></canvas>'));
  app.appendChild(el('div','card','<h2>by language</h2><canvas id="langChart"></canvas>'));

  // Language table (full detail, not just top-N chart).
  const langRows = Object.entries(stats.by_language)
    .sort((a,b)=>b[1].additions-a[1].additions)
    .map(([l,s])=>'<tr><td>'+l+'</td><td class="add">+'+s.additions+'</td><td class="del">-'+s.deletions+'</td><td>'+s.files+'</td></tr>').join('');
  app.appendChild(el('div','card full','<h2>languages (full)</h2><table><tr><th>language</th><th>added</th><th>removed</th><th>files</th></tr>'+langRows+'</table>'));

  // Repo table.
  const repoRows = Object.entries(stats.by_repo)
    .sort((a,b)=>b[1].additions-a[1].additions)
    .map(([r,s])=>'<tr><td>'+r+'</td><td>'+s.commits+'</td><td class="add">+'+s.additions+'</td><td class="del">-'+s.deletions+'</td></tr>').join('');
  app.appendChild(el('div','card full','<h2>by repository</h2><table><tr><th>repo</th><th>commits</th><th>added</th><th>removed</th></tr>'+repoRows+'</table>'));

  // Language detail per repository.
  const rl = stats.by_repo_language || {};
  const detailCard = el('div','card full','<h2>language detail by repository</h2>');
  Object.entries(rl)
    .sort((a,b)=>Object.values(b[1]).reduce((t,v)=>t+v.additions,0)-Object.values(a[1]).reduce((t,v)=>t+v.additions,0))
    .forEach(([repo,langs])=>{
      const rows = Object.entries(langs).sort((a,b)=>b[1].additions-a[1].additions)
        .map(([l,s])=>'<tr><td>'+l+'</td><td class="add">+'+s.additions+'</td><td class="del">-'+s.deletions+'</td><td>'+s.files+'</td></tr>').join('');
      const d = el('details',null,'<summary>'+repo+'</summary><table><tr><th>language</th><th>added</th><th>removed</th><th>files</th></tr>'+rows+'</table>');
      detailCard.appendChild(d);
    });
  app.appendChild(detailCard);

  const palette=['#3fb950','#58a6ff','#e3b341','#f85149','#bc8cff','#39c5cf','#f778ba','#79c0ff'];
  new Chart(document.getElementById('catChart'),{type:'doughnut',
    data:{labels:Object.keys(stats.by_category),datasets:[{data:Object.values(stats.by_category).map(v=>v.additions+v.deletions),backgroundColor:palette}]},
    options:{plugins:{legend:{labels:{color:'#c9d1d9'}}}}});
  const langs=Object.entries(stats.by_language).sort((a,b)=>(b[1].additions+b[1].deletions)-(a[1].additions+a[1].deletions)).slice(0,10);
  new Chart(document.getElementById('langChart'),{type:'bar',
    data:{labels:langs.map(l=>l[0]),datasets:[{label:'lines changed',data:langs.map(l=>l[1].additions+l[1].deletions),backgroundColor:'#58a6ff'}]},
    options:{indexAxis:'y',plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#6e7681'},grid:{color:'#1f2630'}},y:{ticks:{color:'#c9d1d9'},grid:{display:false}}}}});
}
render();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()