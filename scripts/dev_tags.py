#!/usr/bin/env python3
"""
Development-area classification for changed files.

Every changed file gets exactly one high-level "what kind of work is this" area,
which powers the work-split pie charts:

    ai        AI / ML / data science (LLMs, agents, RAG, training, notebooks)
    backend   APIs, services, databases, server-side languages
    devops    CI/CD, containers, IaC, deployment & build tooling
    frontend  UI code, styles, components, mobile UI
    other     docs, lockfiles, generated data, assets — excluded from the pie by default

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
    "other": "Other",
}

# Noise that should never count as "real" work in any area.
_NOISE = [
    r"(^|/)package-lock\.json$", r"(^|/)yarn\.lock$", r"(^|/)pnpm-lock\.yaml$",
    r"\.lock$", r"(^|/)go\.sum$", r"\.min\.(js|css)$", r"(^|/)dist/", r"(^|/)build/",
    r"\.(png|jpe?g|gif|svg|webp|ico|pdf|mp4|woff2?|ttf)$", r"\.(md|mdx|rst|txt)$",
    r"(^|/)license", r"\.(csv|tsv|parquet|jsonl)$",
]

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
    if repo_area and code_lang:
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
    return "other"


# Backwards-compatible alias.
tag_for = area_for
