#!/usr/bin/env python3
"""
Turns fetched commits into statistics (the data/stats.json schema). Local, no network.

    build({repo: [commit records]}, profile, ...) -> stats for those commits
    merge(old_stats, new_stats)                   -> old + new, added together
"""
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from dev_tags import (area_for, commit_type, is_test, repo_default_area, repo_path_area,
                      repo_scope)

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = yaml.safe_load((ROOT / "config" / "categories.yaml").read_text())

# Max lines a single file in a single commit adds to the work-area split, so
# generated dumps (notebook outputs, fixtures, vendored code) can't swamp real work.
AREA_FILE_CAP = 800
# Commits kept per repo for the "Recently" feed.
RECENT_PER_REPO = 15

_DOC_RE = re.compile(r"\.(md|mdx|rst|txt)$|(^|/)docs?/", re.I)

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




# ---------------------------------------------------------------- per-file rows

def classify_rows(files, repo_area, repo=None, repo_paths=None):
    """[[path, add, del], ...] -> [[language, area, is_test, is_doc, add, del], ...]"""
    rows = []
    for path, add, dele in files:
        lang, _ = classify_file(path, CATEGORIES)
        area = area_for(path, lang, repo_path_area(repo or "", path, repo_paths, repo_area))
        rows.append([lang, area, is_test(path) and area != "other", bool(_DOC_RE.search(path)), add, dele])
    return rows


def commit_type_for(message, rows):
    test = sum(r[4] + r[5] for r in rows if r[1] != "other" and r[2])
    code = sum(r[4] + r[5] for r in rows if r[1] != "other" and not r[2])
    docs_only = bool(rows) and all(r[1] == "other" and r[3] for r in rows)
    return commit_type(message, test, code, docs_only)


def _blank():
    return {
        "totals": {"commits": 0, "additions": 0, "deletions": 0},
        "by_repo": {}, "by_language": {}, "by_category": {}, "by_week": {},
        "by_repo_language": {}, "by_area": {}, "by_repo_area": {}, "by_repo_week": {},
        "by_repo_habits": {}, "recent": [],
    }


def _add(bucket, key, add, dele, files=1):
    s = bucket.setdefault(key, {"additions": 0, "deletions": 0, "files": 0})
    s["additions"] += add
    s["deletions"] += dele
    s["files"] += files


# ---------------------------------------------------------------- build

def build(commits, profile, exclude=(), repo_meta=None, legacy=None):
    """Aggregate {repo: [{"sha","date","message","files":[[path,add,del]]}]}."""
    tz = ZoneInfo(profile.get("timezone") or "UTC")
    repo_areas = profile.get("repo_areas") or {}
    repo_paths = profile.get("repo_paths") or {}
    stats = _blank()
    repo_meta = repo_meta or {}

    for repo, records in commits.items():
        if repo in exclude or not records:
            continue
        repo_area = repo_default_area(repo, repo_areas)
        # stats.json is public: never publish messages from private or industry repos.
        hide_msg = (repo_meta.get(repo, {}).get("private", False)
                    or repo_scope(repo, profile) == "professional")
        habit = {"tests": {"test": 0, "code": 0}, "types": {}, "sizes": [],
                 "days": {}, "slots": {}, "lang_months": {}}
        for rec in records:
            rows = classify_rows(rec["files"], repo_area, repo, repo_paths)
            ctype = commit_type_for(rec.get("message", ""), rows)
            cats = [classify_file(p, CATEGORIES)[1] for p, _, _ in rec["files"]]

            when = datetime.fromisoformat(rec["date"].replace("Z", "+00:00")).astimezone(tz)
            day = when.date().isoformat()
            week = (when.date() - timedelta(days=when.weekday())).isoformat()
            month = day[:7]

            stats["totals"]["commits"] += 1
            rstat = stats["by_repo"].setdefault(repo, {"commits": 0, "additions": 0, "deletions": 0})
            rstat["commits"] += 1
            wstat = stats["by_week"].setdefault(week, {"commits": 0, "additions": 0, "deletions": 0})
            wstat["commits"] += 1
            rweeks = stats["by_repo_week"].setdefault(repo, {})
            rweeks[week] = rweeks.get(week, 0) + 1

            areas, c_test, c_code = {}, 0, 0
            for (lang, area, test, _doc, add, dele), cat in zip(rows, cats):
                stats["totals"]["additions"] += add
                stats["totals"]["deletions"] += dele
                for s in (rstat, wstat):
                    s["additions"] += add
                    s["deletions"] += dele
                _add(stats["by_language"], lang, add, dele)
                _add(stats["by_repo_language"].setdefault(repo, {}), lang, add, dele)
                _add(stats["by_category"], cat, add, dele)
                scale = min(1.0, AREA_FILE_CAP / max(add + dele, 1))
                c_add, c_del = round(add * scale), round(dele * scale)
                _add(stats["by_area"], area, c_add, c_del)
                _add(stats["by_repo_area"].setdefault(repo, {}), area, c_add, c_del)
                areas[area] = areas.get(area, 0) + c_add + c_del
                if area != "other":
                    if test:
                        c_test += c_add + c_del
                    else:
                        c_code += c_add + c_del
                    lm = habit["lang_months"].setdefault(lang, {})
                    lm[month] = lm.get(month, 0) + c_add + c_del

            habit["types"][ctype] = habit["types"].get(ctype, 0) + 1
            habit["tests"]["test"] += c_test
            habit["tests"]["code"] += c_code
            habit["sizes"].append(c_test + c_code)
            habit["days"][day] = habit["days"].get(day, 0) + 1
            slot = f"{when.weekday()}-{when.hour}"
            habit["slots"][slot] = habit["slots"].get(slot, 0) + 1
            stats["recent"].append({
                "repo": repo, "sha": rec["sha"][:7], "date": rec["date"], "type": ctype,
                "message": "" if hide_msg else (rec.get("message") or "")[:120],
                "areas": areas,
            })
        stats["by_repo_habits"][repo] = habit

    stats["recent"] = _trim_recent(stats["recent"])

    # Repos in the previous stats.json that weren't fetched this time (e.g.
    # access revoked): carry their aggregates over so they're never lost.
    carried = []
    for repo, rs in (legacy or {}).get("by_repo", {}).items():
        if repo in stats["by_repo"] or repo in exclude:
            continue
        carried.append(repo)
        stats["by_repo"][repo] = dict(rs)
        for k in ("commits", "additions", "deletions"):
            stats["totals"][k] += rs.get(k, 0)
        for key in ("by_repo_language", "by_repo_area", "by_repo_week"):
            if repo in legacy.get(key, {}):
                stats[key][repo] = legacy[key][repo]
        for lang, v in legacy.get("by_repo_language", {}).get(repo, {}).items():
            _add(stats["by_language"], lang, v.get("additions", 0), v.get("deletions", 0), v.get("files", 0))
        for area, v in legacy.get("by_repo_area", {}).get(repo, {}).items():
            _add(stats["by_area"], area, v.get("additions", 0), v.get("deletions", 0), v.get("files", 0))
        for week, n in legacy.get("by_repo_week", {}).get(repo, {}).items():
            stats["by_week"].setdefault(week, {"commits": 0, "additions": 0, "deletions": 0})["commits"] += n
    stats["legacy_repos"] = sorted(carried)
    return stats


def _trim_recent(items):
    """Newest commits per repo for the activity feed (deduped)."""
    recent, per_repo, seen = [], {}, set()
    for c in sorted(items, key=lambda c: c["date"], reverse=True):
        if (c["repo"], c["sha"]) in seen:
            continue
        seen.add((c["repo"], c["sha"]))
        per_repo[c["repo"]] = per_repo.get(c["repo"], 0) + 1
        if per_repo[c["repo"]] <= RECENT_PER_REPO:
            recent.append(c)
    return recent


MERGED_KEYS = ("totals", "by_repo", "by_language", "by_category", "by_week",
               "by_repo_language", "by_area", "by_repo_area", "by_repo_week", "by_repo_habits")


def _sum(a, b):
    """Add b into a copy of a: numbers add, dicts merge recursively, lists extend."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _sum(out[k], v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        return a + b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + b
    return b


def merge(old, new):
    """Existing stats + stats of newly fetched commits. Every stat is a count or
    a sum, so adding them gives the same result as building from all commits."""
    out = dict(old)
    for key in MERGED_KEYS:
        out[key] = _sum(old.get(key, {}), new.get(key, {}))
    out["recent"] = _trim_recent(old.get("recent", []) + new.get("recent", []))
    return out
