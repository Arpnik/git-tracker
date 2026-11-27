#!/usr/bin/env python3
"""
Development-area classification for changed files.

Every changed file gets exactly one high-level "what kind of work is this" area,
which powers the work-split pie charts:

    ai        AI / ML / data science (LLMs, agents, RAG, training, notebooks)
    backend   APIs, services, databases, server-side languages
    devops    CI/CD, containers, IaC, deployment & build tooling
    frontend  UI code, styles, components, mobile UI
    other     docs, lockfiles, data files, assets — excluded from the pie by default

Detection is heuristic: path keywords are checked in priority order (so a
Dockerfile in `backend/` is DevOps, and a Python training script is AI, not
backend), then we fall back to the file's language.
"""
import re

AREAS = ["ai", "backend", "devops", "frontend", "other"]

AREA_LABELS = {
    "ai": "AI / ML",
    "backend": "Backend",
    "devops": "DevOps",
    "frontend": "Frontend",
    "other": "Docs & data",
}

# Noise that should never count as "real" work in any area.
_NOISE = [
    r"(^|/)package-lock\.json$", r"(^|/)yarn\.lock$", r"(^|/)pnpm-lock\.yaml$",
    r"\.lock$", r"(^|/)go\.sum$", r"\.min\.(js|css)$", r"(^|/)dist/", r"(^|/)build/",
    r"\.(png|jpe?g|gif|svg|webp|ico|pdf|mp4|woff2?|ttf)$", r"\.(md|mdx|rst|txt)$",
    r"(^|/)license", r"\.(csv|tsv|parquet|jsonl|ndjson|log)$", r"(^|/)go\.sum$",
    # Data-like JSON/YAML: datasets, fixtures, translations, snapshots, recordings.
    r"(^|/)(data|datasets?|fixtures?|samples?|mocks?|seeds?|snapshots?|locales?|i18n|"
    r"lang|translations?|outputs?|results?|logs?)/.*\.(json|ya?ml|xml)$",
    r"\.(snap|lottie)$", r"(^|/)__snapshots__/",
]

# Config / manifest formats. They carry no area of their own, so they take on the
# repo's discipline (package.json in a React app is frontend work, application.yml
# in a Spring API is backend work). Without a known discipline they are "other".
_CONFIG_LANGS = {
    "JSON", "YAML", "TOML", "XML", "INI", "Config", "Dotenv", "Properties", "Gradle",
    "Groovy", "Plist", "Pbxproj", "Iml",
}

# STRONG signals: the file itself says what it is, regardless of which repo it
# lives in (a Dockerfile in a backend repo is still DevOps work).
_STRONG_RULES = [
    ("devops", [
        r"(^|/)dockerfile", r"docker-compose", r"\.dockerignore$", r"(^|/)\.github/",
        r"\.gitlab-ci", r"(^|/)jenkinsfile", r"\.circleci/", r"azure-pipelines",
        r"bitbucket-pipelines", r"\.tf$", r"\.tfvars$", r"\.hcl$", r"(^|/)terraform/",
        r"(^|/)k8s/", r"(^|/)kubernetes/", r"(^|/)helm/", r"(^|/)ansible/",
        r"(^|/)\.?devcontainer", r"(^|/)nginx", r"(^|/)procfile$", r"(^|/)makefile$",
        r"cloudbuild", r"buildspec", r"serverless\.ya?ml$", r"(^|/)vercel\.json$",
        r"(^|/)netlify\.toml$", r"(^|/)fly\.toml$", r"(^|/)render\.ya?ml$",
        r"(^|/)ecosystem\.config\.js$", r"\.sh$", r"\.ps1$",
    ]),
    ("ai", [r"\.ipynb$", r"\.pkl$", r"\.onnx$", r"\.h5$", r"\.pt$", r"\.safetensors$"]),
    ("frontend", [
        r"\.vue$", r"\.svelte$", r"\.jsx$", r"\.tsx$", r"\.css$", r"\.scss$",
        r"\.sass$", r"\.less$", r"\.html?$", r"tailwind", r"\.storyboard$", r"\.xib$",
    ]),
    ("backend", [
        r"\.sql$", r"(^|/)migrations?/", r"\.proto$", r"\.graphql$", r"\.sol$",
        r"prisma", r"(^|/)knexfile", r"(^|/)sequelize",
    ]),
    # AI keywords in server-side code: an `services/openai.js` in a Node API is AI work.
    ("ai", [
        r"llm", r"openai", r"anthropic", r"langchain", r"langgraph", r"llama", r"prompt",
        r"embedding", r"hugging.?face", r"vector.?(db|store)", r"chroma", r"pinecone",
        r"faiss", r"torch", r"tensorflow", r"keras", r"sklearn",
    ]),
]

# WEAK signals: folder conventions. Only used when the repo has no known
# discipline (a `services/` folder means different things in a Flutter app
# and an Express API).
_WEAK_RULES = [
    ("ai", [
        r"(^|/|_)agents?(/|_|\.)", r"(^|/|_)rag(/|_|\.)", r"transformer", r"neural",
        r"(^|/|_)ml(/|_|\.)", r"(^|/|_)train(ing|er)?(/|_|\.)", r"inference",
        r"notebooks?/", r"dataset", r"classifier", r"(^|/|_)nlp(/|_|\.)",
    ]),
    ("devops", [r"(^|/)deploy(ment)?s?/", r"(^|/)infra(structure)?/", r"(^|/)scripts?/"]),
    ("frontend", [
        r"components?/", r"(^|/)pages?/", r"(^|/)ui/", r"frontend/", r"client/",
        r"(^|/)web/", r"styles?/", r"(^|/)public/", r"(^|/)assets/", r"(^|/)widgets?/",
        r"(^|/)screens?/", r"(^|/)views?/", r"(^|/)hooks/", r"(^|/)lib/.*\.dart$",
    ]),
    ("backend", [
        r"(^|/)api/", r"server/", r"backend/", r"services?/", r"controllers?/",
        r"routes?/", r"handlers?/", r"repositor(y|ies)", r"(^|/)models?/",
        r"(^|/)schemas?/", r"middleware", r"grpc",
    ]),
]

# Language-based fallback when nothing else decided.
_LANG_FALLBACK = {
    "JavaScript": "frontend", "TypeScript": "frontend", "HTML": "frontend",
    "CSS": "frontend", "SCSS": "frontend", "Sass": "frontend", "Less": "frontend",
    "Vue": "frontend", "Svelte": "frontend", "Dart": "frontend", "Swift": "frontend",
    "Objective-C": "frontend",
    "Python": "backend", "Go": "backend", "Java": "backend", "Ruby": "backend",
    "PHP": "backend", "C#": "backend", "Rust": "backend", "Kotlin": "backend",
    "Scala": "backend", "Solidity": "backend", "Vyper": "backend", "C": "backend",
    "C++": "backend", "SQL": "backend", "GraphQL": "backend", "Elixir": "backend",
    "Protocol Buffers": "backend", "Groovy": "backend", "Haskell": "backend",
    "Jupyter Notebook": "ai", "R": "ai", "Julia": "ai",
    "Docker": "devops", "Terraform": "devops", "HCL": "devops", "Shell": "devops",
    "PowerShell": "devops", "Makefile": "devops", "CMake": "devops",
}

# Languages that always mean the same area, whatever the repo (used for the
# language-only estimate as well).
_LANG_FIXED = {
    "Jupyter Notebook": "ai", "CSS": "frontend", "SCSS": "frontend", "Sass": "frontend",
    "Less": "frontend", "HTML": "frontend", "Vue": "frontend", "Svelte": "frontend",
    "Dart": "frontend", "Swift": "frontend", "Objective-C": "frontend",
    "SQL": "backend", "Solidity": "backend", "Vyper": "backend", "Protocol Buffers": "backend",
    "GraphQL": "backend", "Docker": "devops", "Terraform": "devops", "HCL": "devops",
    "Shell": "devops", "PowerShell": "devops", "Makefile": "devops", "CMake": "devops",
}

# Repo-name hints for a repo's main discipline (when profile.yaml doesn't say).
_REPO_NAME_RULES = [
    ("ai", r"(^|[-_.])(ai|ml|llm|rag|nlp|gpt|agents?|agentic|transformers?|bert|neural|"
           r"segmenter|detection|recommender|synth|nl2sql|vision|chatbot)([-_.]|$)"),
    ("devops", r"(^|[-_.])(infra|infrastructure|deploy|devops|ops|terraform|k8s|helm|ci)([-_.]|$)"),
    ("backend", r"(^|[-_.])(back|backend|api|rest|server|service|services|crawler|worker)([-_.]|$)"),
    ("frontend", r"(^|[-_.])(ui|front|frontend|web|client|app|mobile|dashboard|admin|site)([-_.]|$)"),
]

_NOISE_RE = [re.compile(p) for p in _NOISE]
_STRONG = [(a, [re.compile(p) for p in ps]) for a, ps in _STRONG_RULES]
_WEAK = [(a, [re.compile(p) for p in ps]) for a, ps in _WEAK_RULES]
_REPO_NAME = [(a, re.compile(p)) for a, p in _REPO_NAME_RULES]


def repo_scope(full, profile):
    """'professional' (industry) or 'personal' for an owner/repo name."""
    if full in (profile.get("professional_repos") or []):
        return "professional"
    if full in (profile.get("personal_repos") or []):
        return "personal"
    owner = full.split("/")[0].lower()
    orgs = {o.lower() for o in (profile.get("professional_orgs") or {})}
    return "professional" if owner in orgs else "personal"


def scrub_messages(stats, profile):
    """Blank commit messages of industry and private repos in stats["recent"].
    stats.json is public, and industry commit messages must never be published."""
    private = {r for r, m in (stats.get("repo_meta") or {}).items() if m.get("private")}
    for c in stats.get("recent", []):
        if c["repo"] in private or repo_scope(c["repo"], profile) == "professional":
            c["message"] = ""
    return stats


def repo_default_area(full_name, overrides=None):
    """The main discipline of a repo: explicit `repo_areas` override in
    profile.yaml first, then repo-name hints. None if unknown."""
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    if full_name.lower() in overrides:
        return overrides[full_name.lower()]
    name = full_name.split("/")[-1].lower()
    for area, rx in _REPO_NAME:
        if rx.search(name):
            return area
    return None


def repo_path_area(full_name, path, repo_paths=None, default=None):
    """Discipline for one file of a repo, from `repo_paths` in profile.yaml:
    {folder prefix: area}, first match wins, "*" for everything else. Falls back
    to `default` (the repo's discipline) when the repo has no rule."""
    rules = {k.lower(): v for k, v in (repo_paths or {}).items()}.get(full_name.lower())
    if not rules:
        return default
    p = path.lower()
    for prefix, area in rules.items():
        if prefix != "*" and p.startswith(prefix.lower()):
            return area
    return rules.get("*", default)


def area_for(filename, language=None, repo_area=None):
    """Return one development area for a changed file.

    Order: noise -> strong file signals -> the repo's discipline ->
    weak folder conventions -> language fallback.
    """
    path = filename.lower()
    if any(p.search(path) for p in _NOISE_RE):
        return "other"
    for area, patterns in _STRONG:
        if any(p.search(path) for p in patterns):
            return area
    if language in _LANG_FIXED:
        return _LANG_FIXED[language]
    code_lang = language in _LANG_FALLBACK
    if repo_area and (code_lang or language in _CONFIG_LANGS):
        return repo_area
    for area, patterns in _WEAK:
        if any(p.search(path) for p in patterns):
            return area
    if code_lang:
        return _LANG_FALLBACK[language]
    return "other"


def area_for_language(language, repo_area=None):
    """Best guess from language alone (used when no per-file data exists)."""
    if language in _LANG_FIXED:
        return _LANG_FIXED[language]
    if language in _LANG_FALLBACK:
        return repo_area or _LANG_FALLBACK[language]
    if language in _CONFIG_LANGS and repo_area:
        return repo_area
    return "other"


# Backwards-compatible alias.
tag_for = area_for


# ---------------------------------------------------------------- tests

_TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?|e2e|cypress|playwright|integration[-_]?tests?|testing)/"
    r"|\.(test|spec|e2e)\.[a-z0-9]+$"
    r"|(^|/)test_[^/]+\.py$|_test\.(py|go|dart|exs?)$"
    r"|tests?\.(java|kt|cs|swift|scala)$|_spec\.rb$|(^|/)conftest\.py$"
)


def is_test(filename):
    """True for test code (unit, integration, e2e, fixtures setup)."""
    return bool(_TEST_RE.search(filename.lower()))


# ---------------------------------------------------------------- commit types

COMMIT_TYPES = ["feature", "fix", "refactor", "test", "docs", "chore"]
COMMIT_TYPE_LABELS = {
    "feature": "Features & changes", "fix": "Bug fixes", "refactor": "Refactoring",
    "test": "Tests", "docs": "Docs", "chore": "Build, CI & deps",
}
_CONVENTIONAL = re.compile(r"^\s*([a-z]+)(\([^)]*\))?!?:", re.I)
_CONV_MAP = {
    "feat": "feature", "feature": "feature", "fix": "fix", "bugfix": "fix", "hotfix": "fix",
    "refactor": "refactor", "perf": "refactor", "style": "refactor", "cleanup": "refactor",
    "test": "test", "tests": "test", "docs": "docs", "doc": "docs",
    "chore": "chore", "build": "chore", "ci": "chore", "deps": "chore", "revert": "chore",
    "release": "chore",
}
_KEYWORDS = [
    ("fix", r"\b(fix(e[sd])?|bug|hot-?fix|resolve[sd]?|patch(ed)?|crash|issue|broken|error)\b"),
    ("refactor", r"\b(refactor\w*|clean(ed)?[- ]?up|restructur\w*|renam\w*|simplif\w*|"
                 r"reorgani[sz]\w*|extract\w*|optimi[sz]\w*|perf(ormance)?)\b"),
    ("test", r"\b(tests?|testing|unit[- ]?tests?|coverage|spec)\b"),
    ("docs", r"\b(docs?|documentation|readme|comments?)\b"),
    ("chore", r"\b(bump\w*|upgrad\w*|dependenc\w*|deps|ci|workflow|pipeline|docker\w*|"
              r"deploy\w*|release|version|lint\w*|format\w*|config\w*|merge)\b"),
]
_KEYWORDS_RE = [(t, re.compile(p, re.I)) for t, p in _KEYWORDS]


def commit_type(message, test_lines=0, code_lines=0, docs_only=False):
    """Classify a commit as feature/fix/refactor/test/docs/chore.

    Conventional-commit prefixes win, then what the diff touched (only tests /
    only docs), then message keywords. Anything else counts as feature work.
    """
    m = _CONVENTIONAL.match(message or "")
    if m and m.group(1).lower() in _CONV_MAP:
        return _CONV_MAP[m.group(1).lower()]
    if test_lines and not code_lines:
        return "test"
    if docs_only:
        return "docs"
    for t, rx in _KEYWORDS_RE:
        if rx.search(message or ""):
            return t
    return "feature"
