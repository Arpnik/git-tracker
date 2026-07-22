#!/usr/bin/env python3
"""
Development-area tagging for changed files.

Assigns a single high-level "what kind of work is this" tag to each file so the
collector can report stats per area on top of the finer-grained language/category
breakdowns.

Tags: ai, ml, datascience, frontend, backend, other

Detection is best-effort and heuristic: path/name keywords are checked first
(so a Python training script is tagged `ml`, not `backend`), then we fall back
to the file's language.
"""
import re

# The full, ordered set of tags we can emit (highest priority first).
TAGS = ["ai", "ml", "datascience", "frontend", "backend", "other"]

# (tag, [regex]) — first tag whose pattern matches the lowercased path wins.
_KEYWORD_RULES = [
    ("ai", [
        r"llm", r"gpt", r"openai", r"anthropic", r"claude", r"langchain",
        r"transformer", r"\bprompt", r"\bagent", r"\brag\b", r"embedding",
        r"\bbert\b", r"hugging.?face", r"neural", r"\bnlp\b", r"torch",
        r"tensorflow", r"keras", r"diffusion", r"genai", r"vector.?db",
    ]),
    ("ml", [
        r"\bml\b", r"machine.?learning", r"sklearn", r"scikit", r"xgboost",
        r"lightgbm", r"catboost", r"random.?forest", r"regression",
        r"classifier", r"\btrain(ing|er)?\b", r"(^|/|_)model", r"\.pkl$",
        r"\.onnx$", r"\.h5$", r"feature.?eng", r"inference",
    ]),
    ("datascience", [
        r"\.ipynb$", r"notebook", r"pandas", r"numpy", r"\.csv$", r"\.parquet$",
        r"\.tsv$", r"dataset", r"\beda\b", r"analysis", r"analytics",
        r"matplotlib", r"seaborn", r"plotly", r"\betl\b", r"pipeline",
        r"warehouse", r"\bdbt\b",
    ]),
    ("frontend", [
        r"components?/", r"pages?/", r"(^|/)ui/", r"frontend/", r"client/",
        r"(^|/)web/", r"\.vue$", r"\.svelte$", r"\.jsx$", r"\.tsx$", r"\.css$",
        r"\.scss$", r"\.less$", r"\.html?$", r"tailwind", r"styles?/",
    ]),
    ("backend", [
        r"(^|/)api/", r"server/", r"backend/", r"services?/", r"controllers?/",
        r"routes?/", r"handlers?/", r"repository", r"\.sql$", r"migrations?/",
        r"grpc", r"\.proto$", r"\.go$", r"\.java$", r"\.rb$", r"\.php$",
        r"\.cs$", r"\.rs$",
    ]),
]

# Language-based fallback when no keyword matched.
_LANG_FALLBACK = {
    "JavaScript": "frontend", "TypeScript": "frontend", "HTML": "frontend",
    "CSS": "frontend", "SCSS": "frontend", "Sass": "frontend", "Less": "frontend",
    "Vue": "frontend", "Svelte": "frontend",
    "Python": "backend", "Go": "backend", "Java": "backend", "Ruby": "backend",
    "PHP": "backend", "C#": "backend", "Rust": "backend", "Kotlin": "backend",
    "Scala": "backend", "Solidity": "backend", "C": "backend", "C++": "backend",
    "Jupyter Notebook": "datascience", "R": "datascience", "SQL": "datascience",
}

_COMPILED = [(tag, [re.compile(p) for p in pats]) for tag, pats in _KEYWORD_RULES]


def tag_for(filename, language=None):
    """Return one development-area tag for a file path (+ optional language)."""
    path = filename.lower()
    for tag, patterns in _COMPILED:
        if any(p.search(path) for p in patterns):
            return tag
    if language and language in _LANG_FALLBACK:
        return _LANG_FALLBACK[language]
    return "other"

