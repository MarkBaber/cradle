"""AST-based layering enforcement (stdlib fallback for import-linter).

Rule (SPEC 3): routers -> services -> (reference|alerts|repos|ports) -> models.
reference and alerts import only models + stdlib.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cradle"

# layer -> set of cradle.* layers it may import
ALLOWED: dict[str, set[str]] = {
    "models": set(),
    "reference": {"models"},
    "alerts": {"models"},
    "repos": {"models"},
    "ports": {"models"},
    "services": {"models", "reference", "alerts", "repos", "ports"},
    "routers": {"models", "services"},
}


def _layer_of(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "cradle" and parts[1] in ALLOWED:
        return parts[1]
    return None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_layering() -> None:
    violations: list[str] = []
    for layer, allowed in ALLOWED.items():
        for py in (SRC / layer).rglob("*.py"):
            for mod in _imports(py):
                target = _layer_of(mod)
                if target and target != layer and target not in allowed:
                    violations.append(f"{py.relative_to(SRC)} imports {mod}")
    assert not violations, "Layering violations:\n" + "\n".join(violations)
