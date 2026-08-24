"""Import hygiene rules for deaddit/domain (scanned when it appears in Wave 3)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_DOMAIN_DIR = Path(__file__).parent.parent / "deaddit" / "domain"

_BANNED_FLASK_IMPORTS = {"request", "session", "current_app"}


def _python_files() -> list[Path]:
    if not _DOMAIN_DIR.is_dir():
        return []
    return sorted(_DOMAIN_DIR.rglob("*.py"))


def _iter_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield node
        elif isinstance(node, ast.Import):
            yield node


@pytest.mark.skipif(
    not _DOMAIN_DIR.is_dir(), reason="deaddit/domain not present yet"
)
@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_domain_import_rules(path: Path) -> None:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in _iter_imports(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            # from x import *  ->  alias list contains a single star alias.
            for alias in node.names:
                assert alias.name != "*", (
                    f"{path}: 'from {node.module} import *' is banned "
                    "in deaddit/domain"
                )
            if node.module == "flask":
                banned = [a.name for a in node.names if a.name in _BANNED_FLASK_IMPORTS]
                assert not banned, (
                    f"{path}: flask context objects {banned} are banned "
                    "in deaddit/domain"
                )
        else:
            assert not any(a.name == "*" for a in node.names), (
                f"{path}: 'import *' is banned in deaddit/domain"
            )
