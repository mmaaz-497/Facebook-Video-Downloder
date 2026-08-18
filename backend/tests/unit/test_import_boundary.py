"""Constitutional gate: yt_dlp may be imported ONLY in app/services/extractor.py.

Walks every module AST under backend/app so any new import of yt_dlp anywhere
else is a blocking failure, forever.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
ALLOWED = APP_ROOT / "services" / "extractor.py"


def _yt_dlp_imports(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits.extend(
                a.name for a in node.names if a.name.split(".")[0] == "yt_dlp"
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "yt_dlp":
                hits.append(node.module)
    return hits


def test_yt_dlp_imported_only_in_extractor():
    offenders: dict[str, list[str]] = {}
    py_files = sorted(APP_ROOT.rglob("*.py"))
    assert py_files, f"no python files found under {APP_ROOT}"
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _yt_dlp_imports(tree)
        if hits and path.resolve() != ALLOWED.resolve():
            offenders[str(path.relative_to(APP_ROOT))] = hits
    assert not offenders, f"yt_dlp imported outside the extractor wrapper: {offenders}"
