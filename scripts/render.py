#!/usr/bin/env python3
"""
Renders everything people see from data/stats.json + config/profile.yaml:

  README.md               the full profile page (also synced to <user>/<user>)
  assets/work-split-*.svg auto-updating donut charts used by the README
  assets/activity.svg     weekly commit activity, professional vs personal
  index.html              interactive dashboard (data embedded; GitHub Pages entry)
  README_STATS.md         full tables (languages, repos, per-repo detail)

Needs no GitHub token, so you can preview locally:

    python scripts/render.py
"""
import json
import math
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

import yaml

from dev_tags import AREAS, AREA_LABELS, area_for_language, repo_default_area

ROOT = Path(__file__).resolve().parent.parent
STATS_FILE = ROOT / "data" / "stats.json"
PROFILE_FILE = ROOT / "config" / "profile.yaml"
TEMPLATE_FILE = ROOT / "dashboard" / "template.html"
ASSETS = ROOT / "assets"
REPO_URL = "https://github.com/Arpnik/git-tracker"

SCOPES = ["all", "professional", "personal"]
SCOPE_LABELS = {"all": "All work", "professional": "Professional", "personal": "Personal"}
PIE_AREAS = [a for a in AREAS if a != "other"]
AREA_EMOJI = {"ai": "🧠", "backend": "⚙️", "devops": "🚀", "frontend": "🎨", "other": "📦"}

# Categorical slots 1-4 (light / dark) — fixed per area, never by rank.
AREA_COLORS = {
    "ai":       ("#257de0", "#338ced"),
    "backend":  ("#f47230", "#e76624"),
    "devops":   ("#00b894", "#00a888"),
    "frontend": ("#f2a600", "#d28e05"),
    "other":    ("#9c9c9c", "#6b6b6b"),
}
# Professional vs personal in the activity chart: neutral inks, not area hues.
SCOPE_COLORS = {"professional": ("#1f2328", "#e6edf3"), "personal": ("#8c959f", "#656c76")}

# Languages that aren't "real" code when estimating areas from language totals.
_NOISE_LANGS = {"JSON", "Lock", "Markdown", "Text", "CSV", "Image", "PDF", "Config",
                "Dotenv", "YAML", "TOML", "INI", "XML", "JSON Lines", "reStructuredText"}

FONT = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"


# ---------------------------------------------------------------- data helpers

def load_profile():
    p = yaml.safe_load(PROFILE_FILE.read_text()) if PROFILE_FILE.exists() else {}
    p = p or {}
    for k in ("professional_orgs", "links", "repo_areas", "featured", "activity"):
        p[k] = p.get(k) or {}
    p["about"] = p.get("about") or []
    return p


def dashboard_url(profile):
    return profile.get("dashboard_url") or profile["links"].get("Dashboard") or ""


def repo_scope(full, profile):
    if full in (profile.get("professional_repos") or []):
        return "professional"
    if full in (profile.get("personal_repos") or []):
        return "personal"
    owner = full.split("/")[0].lower()
    orgs = {o.lower() for o in profile["professional_orgs"]}
    return "professional" if owner in orgs else "personal"


def org_meta(owner, profile):
    for org, meta in profile["professional_orgs"].items():
        if org.lower() == owner.lower():
            return org, meta or {}
    return owner, {}


def ensure_area_data(stats, profile):
    """Older stats.json files have no per-file area split. Estimate one from the
    per-repo language totals + each repo's discipline so charts work right away;
    the next collector run replaces it with exact per-file numbers."""
    if stats.get("by_repo_area") and not stats.get("area_estimated"):
        return stats
    by_repo_area = {}
    for repo, langs in stats.get("by_repo_language", {}).items():
        repo_area = repo_default_area(repo, profile["repo_areas"])
        ra = by_repo_area.setdefault(repo, {})
        for lang, s in langs.items():
            area = "other" if lang in _NOISE_LANGS else area_for_language(lang, repo_area)
            a = ra.setdefault(area, {"additions": 0, "deletions": 0, "files": 0})
            # Mirror the collector's per-file cap (800 lines) on aggregates.
            cap = 800 * max(s.get("files", 1), 1)
            scale = min(1.0, cap / max(lines(s), 1))
            for k in ("additions", "deletions"):
                a[k] += round(s.get(k, 0) * scale)
            a["files"] += s.get("files", 0)
    stats["by_repo_area"] = by_repo_area
    stats["area_estimated"] = True
    return stats


def lines(s):
    return s.get("additions", 0) + s.get("deletions", 0)


def area_totals(stats, profile, scope, repos=None):
    out = {a: 0 for a in AREAS}
    for repo, areas in stats.get("by_repo_area", {}).items():
        if repos is not None and repo not in repos:
            continue
        if scope != "all" and repo_scope(repo, profile) != scope:
            continue
        for a, s in areas.items():
            out[a if a in out else "other"] += lines(s)
    return out


def scope_summary(stats, profile, scope):
    repos = [r for r in stats["by_repo"] if scope == "all" or repo_scope(r, profile) == scope]
    return {
        "repos": len(repos),
        "commits": sum(stats["by_repo"][r]["commits"] for r in repos),
        "lines": sum(lines(stats["by_repo"][r]) for r in repos),
    }


def repo_link(stats, repo, text=None):
    """Markdown link to a repo, or plain code text if it's private (a recruiter
    would just hit a 404)."""
    text = text or f"`{repo}`"
    if stats.get("repo_meta", {}).get(repo, {}).get("private"):
        return text
    return f"[{text}](https://github.com/{repo})"


def primary_area(stats, repo):
    areas = {a: lines(s) for a, s in stats.get("by_repo_area", {}).get(repo, {}).items()
             if a != "other"}
    return max(areas, key=areas.get) if areas and max(areas.values()) > 0 else "other"


def area_mix_text(totals, top=3):
    tot = sum(totals.get(a, 0) for a in PIE_AREAS)
    if not tot:
        return ""
    parts = sorted(((a, totals.get(a, 0)) for a in PIE_AREAS), key=lambda x: -x[1])
    return " · ".join(f"{AREA_LABELS[a]} {round(100 * v / tot)}%" for a, v in parts[:top] if v)


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def now_of(stats):
    try:
        return datetime.fromisoformat(stats["generated_at"])
    except (KeyError, ValueError):
        return datetime.now(timezone.utc)


def weekly_by_scope(stats, profile, n_weeks=26):
    """[(monday, professional_commits, personal_commits)] for the last n weeks.
    Falls back to the all-repo weekly series (as 'personal'=0) on old data."""
    today = now_of(stats).date()
    monday = today - timedelta(days=today.weekday())
    weeks = [(monday - timedelta(weeks=i)).isoformat() for i in range(n_weeks - 1, -1, -1)]
    pro = {w: 0 for w in weeks}
    per = {w: 0 for w in weeks}
    split = bool(stats.get("by_repo_week"))
    if split:
        for repo, wk in stats["by_repo_week"].items():
            bucket = pro if repo_scope(repo, profile) == "professional" else per
            for w, c in wk.items():
                if w in bucket:
                    bucket[w] += c
    else:
        for w, s in stats.get("by_week", {}).items():
            if w in per:
                per[w] += s.get("commits", 0)
    return [(w, pro[w], per[w]) for w in weeks], split


# ---------------------------------------------------------------- SVGs

def _svg_open(W, H, label, extra_css=""):
    css_light = "".join(f".s-{a}{{fill:{AREA_COLORS[a][0]}}}" for a in AREAS)
    css_dark = "".join(f".s-{a}{{fill:{AREA_COLORS[a][1]}}}" for a in AREAS)
    sc_light = "".join(f".k-{s}{{fill:{c[0]}}}" for s, c in SCOPE_COLORS.items())
    sc_dark = "".join(f".k-{s}{{fill:{c[1]}}}" for s, c in SCOPE_COLORS.items())
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">',
        "<style>"
        f".t{{font:600 15px {FONT};fill:#1f2328}}"
        f".st,.lv,.ax{{font:12px {FONT};fill:#59636e}}"
        f".ln{{font:500 13px {FONT};fill:#1f2328}}"
        f".c{{font:600 18px {FONT};fill:#1f2328}}"
        ".grid{stroke:#eaeef2}"
        ".gap{stroke:#ffffff}.bg{fill:#ffffff;stroke:#d1d9e0}" + css_light + sc_light + extra_css +
        "@media (prefers-color-scheme:dark){"
        ".t,.ln,.c{fill:#e6edf3}.st,.lv,.ax{fill:#9198a1}.grid{stroke:#212830}"
        ".gap{stroke:#151b23}.bg{fill:#151b23;stroke:#30363d}" + css_dark + sc_dark + "}"
        "</style>",
        f'<rect class="bg" x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12"/>',
    ]


def donut_svg(totals, title, subtitle):
    """Donut of the four work areas ('other' excluded) with a direct-labelled legend.
    Has its own card background so it reads on GitHub in light and dark."""
    W, H = 460, 240
    cx, cy, r_out, r_in = 110, 146, 80, 51
    vals = [(a, totals.get(a, 0)) for a in PIE_AREAS]
    total = sum(v for _, v in vals)
    parts = _svg_open(W, H, title)
    parts += [f'<text class="t" x="16" y="28">{escape(title)}</text>',
              f'<text class="st" x="16" y="46">{escape(subtitle)}</text>']

    if total == 0:
        parts.append(f'<text class="st" x="{cx}" y="{cy}" text-anchor="middle">no data yet</text>')
    else:
        angle = -math.pi / 2
        for a, v in vals:
            if v <= 0:
                continue
            sweep = 2 * math.pi * v / total
            if sweep >= 2 * math.pi - 1e-6:
                parts.append(f'<circle class="s-{a}" cx="{cx}" cy="{cy}" r="{r_out}"/>')
                parts.append(f'<circle class="bg" cx="{cx}" cy="{cy}" r="{r_in}"/>')
            else:
                end = angle + sweep
                large = 1 if sweep > math.pi else 0
                p = lambda r, t: (cx + r * math.cos(t), cy + r * math.sin(t))  # noqa: E731
                x1, y1 = p(r_out, angle); x2, y2 = p(r_out, end)
                x3, y3 = p(r_in, end); x4, y4 = p(r_in, angle)
                parts.append(
                    f'<path class="s-{a} gap" stroke-width="2" d="M{x1:.2f},{y1:.2f} '
                    f'A{r_out},{r_out} 0 {large} 1 {x2:.2f},{y2:.2f} L{x3:.2f},{y3:.2f} '
                    f'A{r_in},{r_in} 0 {large} 0 {x4:.2f},{y4:.2f} Z"/>')
            angle += sweep
        parts.append(f'<text class="c" x="{cx}" y="{cy + 2}" text-anchor="middle">{fmt(total)}</text>')
        parts.append(f'<text class="st" x="{cx}" y="{cy + 18}" text-anchor="middle">lines</text>')

    for i, (a, v) in enumerate(vals):
        y = 92 + i * 36
        pct = f"{100 * v / total:.0f}%" if total else "–"
        parts.append(f'<rect class="s-{a}" x="236" y="{y - 11}" width="12" height="12" rx="3"/>')
        parts.append(f'<text class="ln" x="256" y="{y}">{AREA_LABELS[a]}</text>')
        parts.append(f'<text class="ln" x="440" y="{y}" text-anchor="end">{pct}</text>')
        parts.append(f'<text class="lv" x="256" y="{y + 15}">{fmt(v)} lines</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def activity_svg(stats, profile):
    """Stacked weekly commit bars for the last 26 weeks: professional + personal."""
    weeks, split = weekly_by_scope(stats, profile)
    W, H = 920, 230
    left, right, top, bottom = 44, 16, 64, 34
    pw, ph = W - left - right, H - top - bottom
    peak = max((p + q for _, p, q in weeks), default=0)
    step = 10 if peak > 20 else 2
    ymax = max(4, int(math.ceil(peak / step)) * step)
    total = sum(p + q for _, p, q in weeks)
    parts = _svg_open(W, H, "Weekly commit activity")
    parts.append('<text class="t" x="16" y="28">Commit activity · last 26 weeks</text>')
    parts.append(f'<text class="st" x="16" y="46">{total:,} commits</text>')
    if split:
        lx = W - 16
        for s in ("personal", "professional"):
            label = SCOPE_LABELS[s]
            tw = 7 * len(label) + 4
            parts.append(f'<text class="ln" x="{lx}" y="32" text-anchor="end">{label}</text>')
            parts.append(f'<rect class="k-{s}" x="{lx - tw - 18}" y="22" width="12" height="12" rx="3"/>')
            lx -= tw + 36

    for frac in (0, 0.5, 1):
        y = top + ph - ph * frac
        parts.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="ax" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{int(ymax * frac)}</text>')

    slot = pw / len(weeks)
    bw = max(4, min(22, slot - 6))
    last_month = None
    for i, (w, p, q) in enumerate(weeks):
        x = left + i * slot + (slot - bw) / 2
        base = top + ph
        for s, v in (("professional", p), ("personal", q)):
            if v <= 0:
                continue
            h = ph * v / ymax
            parts.append(f'<rect class="k-{s}" x="{x:.1f}" y="{base - h:.1f}" width="{bw:.1f}" '
                         f'height="{max(h - 1, 1):.1f}" rx="2"/>')
            base -= h + 1  # 1px surface gap between stacked segments
        d = datetime.fromisoformat(w)
        if d.month != last_month:
            parts.append(f'<text class="ax" x="{x + bw / 2:.1f}" y="{H - 12}" text-anchor="middle">'
                         f'{d.strftime("%b")}</text>')
            last_month = d.month
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- README

def badge(label, message, color, url, logo=None):
    src = (f"https://img.shields.io/badge/{quote(label.replace('-', '--'))}-"
           f"{quote(message.replace('-', '--'))}-{color}?style=for-the-badge"
           + (f"&logo={logo}&logoColor=white" if logo else ""))
    return f'<a href="{escape(url)}"><img alt="{escape(label)}" src="{src}"></a>'


LINK_STYLE = {
    "github": ("181717", "github"), "linkedin": ("0A66C2", "linkedin"),
    "email": ("EA4335", "gmail"), "dashboard": ("257de0", None),
    "portfolio": ("1f2328", None), "website": ("1f2328", None),
}


def recent_activity(stats, profile, n):
    show_msgs = profile["activity"].get("show_professional_messages", False)
    groups = []
    for c in stats.get("recent", []):
        day = c["date"][:10]
        if groups and groups[-1]["day"] == day and groups[-1]["repo"] == c["repo"]:
            g = groups[-1]
        else:
            g = {"day": day, "repo": c["repo"], "count": 0, "msg": c["message"], "areas": {}}
            groups.append(g)
        g["count"] += 1
        for a, v in c.get("areas", {}).items():
            g["areas"][a] = g["areas"].get(a, 0) + v
        if len(groups) > n:
            groups.pop()
            break
    out = []
    for g in groups[:n]:
        d = datetime.fromisoformat(g["day"]).strftime("%b %d")
        area = max((a for a in g["areas"] if a != "other"), key=g["areas"].get, default="other")
        tag = f"{AREA_EMOJI[area]} {AREA_LABELS[area]}"
        commits = f"{g['count']} commit{'s' if g['count'] > 1 else ''}"
        repo = g["repo"]
        if repo_scope(repo, profile) == "professional":
            org, meta = org_meta(repo.split("/")[0], profile)
            where = f"💼 **{meta.get('display', org)}** · `{repo.split('/', 1)[1]}`"
            msg = f" · “{escape(g['msg'])}”" if show_msgs and g["msg"] else ""
        else:
            where = repo_link(stats, repo)
            msg = f" · “{escape(g['msg'])}”" if g["msg"] else ""
        out.append(f"- **{d}** · {where} · {commits}{msg} · {tag}")
    return out


def readme(stats, profile):
    dash = dashboard_url(profile)
    gen = now_of(stats).strftime("%b %d, %Y")
    o = []

    # --- Header
    o.append(f'<h1>Hi, I\'m {escape(profile.get("name", ""))} 👋</h1>')
    o.append("")
    if profile.get("headline"):
        o.append(f"**{profile['headline']}**")
        o.append("")
    badges = []
    for k, url in profile["links"].items():
        color, logo = LINK_STYLE.get(k.lower(), ("1f2328", None))
        msg = "open interactive view" if k.lower() == "dashboard" else url.split("/")[-1] or k
        if k.lower() == "email":
            msg = "say hi"
        badges.append(badge(k, msg, color, url, logo))
    if badges:
        o.append("<p>\n  " + "\n  ".join(badges) + "\n</p>")
        o.append("")
    for line in profile["about"]:
        o.append(f"- {line}")
    o.append("")

    # --- Headline numbers
    allm, prom, perm = (scope_summary(stats, profile, s) for s in SCOPES)
    weeks, _ = weekly_by_scope(stats, profile, 4)
    last4 = sum(p + q for _, p, q in weeks)
    o.append(f"> **{allm['commits']:,} commits** across **{allm['repos']} repositories**: "
             f"{prom['repos']} professional, {perm['repos']} personal · "
             f"**{last4} commits in the last 4 weeks**")
    o.append("")

    # --- Work split with a native toggle (<details>)
    o.append("## 🧭 What I work on")
    o.append("")
    o.append("Every changed file in my commits is classified as **AI / ML**, **Backend**, **DevOps** or "
             "**Frontend**, then split by **professional** (company orgs) vs **personal** work. "
             "Expand a view below" + (f", or [open the interactive dashboard]({dash}) to toggle areas "
                                      "and click into repos." if dash else "."))
    o.append("")
    for s, open_ in (("all", True), ("professional", False), ("personal", False)):
        m = scope_summary(stats, profile, s)
        t = area_totals(stats, profile, s)
        tot = sum(t[a] for a in PIE_AREAS) or 1
        icon = {"all": "🌐", "professional": "💼", "personal": "🧪"}[s]
        name = "Both" if s == "all" else SCOPE_LABELS[s]
        o.append(f"<details{' open' if open_ else ''}>")
        o.append(f"<summary><b>{icon} {name}</b> · {m['repos']} repos · {m['commits']:,} commits "
                 f"· {area_mix_text(t)}</summary>")
        o.append("<br>")
        o.append("")
        o.append(f'<img src="./assets/work-split-{s}.svg" alt="{name} work split" width="460">')
        o.append("")
        o.append("| Area | Share | Lines changed |" + (" |" if dash else ""))
        o.append("|---|--:|--:|" + ("---|" if dash else ""))
        for a in PIE_AREAS:
            row = f"| {AREA_EMOJI[a]} {AREA_LABELS[a]} | {round(100 * t[a] / tot)}% | {t[a]:,} |"
            if dash:
                row += f" [explore →]({dash}#scope={s}&area={a}) |"
            o.append(row)
        o.append("")
        o.append("</details>")
        o.append("")

    # --- Activity
    o.append("## 📈 Activity")
    o.append("")
    o.append('<img src="./assets/activity.svg" alt="Weekly commit activity" width="100%">')
    o.append("")
    feed = recent_activity(stats, profile, int(profile["activity"].get("items", 8)))
    if feed:
        o.append("**Recently**")
        o.append("")
        o += feed
    else:
        o.append("<sub>The recent-commit feed fills in after the next scheduled run.</sub>")
    o.append("")

    # --- Professional experience
    o.append("## 💼 Professional experience")
    o.append("")
    by_org = {}
    for repo in stats["by_repo"]:
        if repo_scope(repo, profile) == "professional":
            by_org.setdefault(repo.split("/")[0], []).append(repo)
    if not by_org:
        o.append("_No professional repos detected yet. Add your company orgs in `config/profile.yaml`._")
    for owner, repos in sorted(by_org.items(),
                               key=lambda kv: -sum(stats["by_repo"][r]["commits"] for r in kv[1])):
        org, meta = org_meta(owner, profile)
        name = meta.get("display", org)
        commits = sum(stats["by_repo"][r]["commits"] for r in repos)
        t = area_totals(stats, profile, "all", set(repos))
        o.append(f"### {name}" + (f" · {meta['role']}" if meta.get("role") else ""))
        if meta.get("summary"):
            o.append(f"{meta['summary']}  ")
        o.append(f"<sub>{len(repos)} repos · {commits:,} commits · {area_mix_text(t, 4)}</sub>")
        o.append("")
        o.append("| Repository | Discipline | Commits | Lines changed |")
        o.append("|---|---|--:|--:|")
        for r in sorted(repos, key=lambda x: -stats["by_repo"][x]["commits"]):
            s = stats["by_repo"][r]
            a = primary_area(stats, r)
            o.append(f"| `{r.split('/', 1)[1]}` | {AREA_EMOJI[a]} {AREA_LABELS[a]} "
                     f"| {s['commits']:,} | {lines(s):,} |")
        o.append("")

    # --- Featured / personal projects
    o.append("## 🧪 Projects")
    o.append("")
    o.append("| Project | What it is | Discipline | Commits |")
    o.append("|---|---|---|--:|")
    personal = [r for r in stats["by_repo"] if repo_scope(r, profile) == "personal"]
    featured = [r for r in profile["featured"] if r in stats["by_repo"]]
    rest = sorted((r for r in personal if r not in featured),
                  key=lambda x: -stats["by_repo"][x]["commits"])
    for r in featured + rest[:max(0, 10 - len(featured))]:
        a = primary_area(stats, r)
        meta = stats.get("repo_meta", {}).get(r, {})
        desc = profile["featured"].get(r) or meta.get("description") or "–"
        star = "⭐ " if r in profile["featured"] else ""
        name = repo_link(stats, r, f"**{r.split('/', 1)[1]}**")
        if not r.startswith(profile["links"].get("GitHub", "").rstrip("/").split("/")[-1] + "/"):
            name += f" <sub>with {r.split('/')[0]}</sub>"
        o.append(f"| {star}{name} | {escape(desc)} "
                 f"| {AREA_EMOJI[a]} {AREA_LABELS[a]} | {stats['by_repo'][r]['commits']:,} |")
    hidden = len(personal) - len(featured) - max(0, 10 - len(featured))
    if hidden > 0:
        o.append("")
        o.append(f"<sub>+ {hidden} more in the [full stats]({REPO_URL}/blob/main/README_STATS.md)"
                 + (f" and the [dashboard]({dash})" if dash else "") + ".</sub>")
    o.append("")

    # --- Footer
    note = " · area split is estimated from languages until the next full run" if stats.get("area_estimated") else ""
    o.append("---")
    o.append(f"<sub>🤖 This page regenerates daily from my commit history by "
             f"[git-tracker]({REPO_URL}) ([how it works]({REPO_URL}/blob/main/docs/HOW_IT_WORKS.md)) "
             f"· last updated {gen}{note}</sub>")
    return "\n".join(o) + "\n"


def write_stats_md(stats, profile):
    out = ["# Full dev stats\n", f"_Last updated: {stats.get('generated_at', '')}_\n",
           f"← [Back to profile]({REPO_URL})\n"]
    t = stats["totals"]
    out.append(f"**Totals:** {t['commits']:,} commits · +{t['additions']:,} / -{t['deletions']:,} lines\n")

    out.append("## By work area\n")
    out.append("| Area | Professional | Personal | All |")
    out.append("|---|--:|--:|--:|")
    tots = {s: area_totals(stats, profile, s) for s in SCOPES}
    for a in AREAS:
        out.append(f"| {AREA_LABELS[a]} | {tots['professional'][a]:,} | "
                   f"{tots['personal'][a]:,} | {tots['all'][a]:,} |")

    out.append("\n## By language\n")
    out.append("| Language | + | - | Files touched |")
    out.append("|---|--:|--:|--:|")
    for lang, s in sorted(stats["by_language"].items(), key=lambda x: -x[1]["additions"])[:25]:
        out.append(f"| {lang} | {s['additions']:,} | {s['deletions']:,} | {s['files']:,} |")

    out.append("\n## By repository\n")
    out.append("| Repo | Type | Discipline | Commits | + | - |")
    out.append("|---|---|---|--:|--:|--:|")
    for repo, s in sorted(stats["by_repo"].items(), key=lambda x: -x[1]["commits"]):
        kind = "💼 Professional" if repo_scope(repo, profile) == "professional" else "Personal"
        out.append(f"| {repo} | {kind} | {AREA_LABELS[primary_area(stats, repo)]} "
                   f"| {s['commits']:,} | {s['additions']:,} | {s['deletions']:,} |")

    out.append("\n## Language detail by repository\n")
    for repo, langs in sorted(stats.get("by_repo_language", {}).items(),
                              key=lambda x: -sum(v["additions"] for v in x[1].values())):
        out.append(f"\n<details><summary><strong>{repo}</strong></summary>\n")
        out.append("| Language | + | - | Files touched |")
        out.append("|---|--:|--:|--:|")
        for lang, s in sorted(langs.items(), key=lambda x: -x[1]["additions"]):
            out.append(f"| {lang} | {s['additions']:,} | {s['deletions']:,} | {s['files']:,} |")
        out.append("\n</details>")
    (ROOT / "README_STATS.md").write_text("\n".join(out) + "\n")


# ---------------------------------------------------------------- dashboard

def write_dashboard(stats, profile):
    repos = {}
    for r, s in stats["by_repo"].items():
        owner = r.split("/")[0]
        org, meta = org_meta(owner, profile)
        repos[r] = {
            "scope": repo_scope(r, profile),
            "private": stats.get("repo_meta", {}).get(r, {}).get("private", False),
            "org": meta.get("display", org) if meta else owner,
            "commits": s["commits"], "additions": s["additions"], "deletions": s["deletions"],
            "areas": {a: lines(v) for a, v in stats.get("by_repo_area", {}).get(r, {}).items()},
            "weeks": stats.get("by_repo_week", {}).get(r, {}),
            "languages": {l: lines(v) for l, v in stats.get("by_repo_language", {}).get(r, {}).items()},
        }
    payload = {
        "generated_at": stats.get("generated_at"),
        "area_estimated": bool(stats.get("area_estimated")),
        "profile": {
            "name": profile.get("name", ""), "headline": profile.get("headline", ""),
            "links": {k: v for k, v in profile["links"].items() if k.lower() != "dashboard"},
            "orgs": {meta.get("display", org): {k: meta.get(k) for k in ("role", "summary", "url")}
                     for org, meta in profile["professional_orgs"].items() if meta is not None},
        },
        "areas": {a: {"label": AREA_LABELS[a], "light": AREA_COLORS[a][0], "dark": AREA_COLORS[a][1]}
                  for a in AREAS},
        "by_week": stats.get("by_week", {}),
        "repos": repos,
    }
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE_FILE.read_text().replace("/*__STATS_JSON__*/null", blob)
    (ROOT / "index.html").write_text(html)


def render_all(stats):
    profile = load_profile()
    stats = ensure_area_data(stats, profile)
    ASSETS.mkdir(exist_ok=True)
    for s in SCOPES:
        m = scope_summary(stats, profile, s)
        sub = f"{m['repos']} repos · {m['commits']:,} commits · excludes docs/lockfiles"
        name = "Both" if s == "all" else SCOPE_LABELS[s]
        (ASSETS / f"work-split-{s}.svg").write_text(
            donut_svg(area_totals(stats, profile, s), f"{name} — work split", sub))
    (ASSETS / "activity.svg").write_text(activity_svg(stats, profile))
    (ROOT / "README.md").write_text(readme(stats, profile))
    write_stats_md(stats, profile)
    write_dashboard(stats, profile)


if __name__ == "__main__":
    render_all(json.loads(STATS_FILE.read_text()))
    print("Rendered README.md, assets/*.svg, index.html, README_STATS.md")
