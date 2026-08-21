"""Guard: every route module imported by app.py must be committed to git."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PY = REPO_ROOT / "cleanplex" / "web" / "app.py"
ROUTES_DIR = REPO_ROOT / "cleanplex" / "web" / "routes"

_ROUTE_IMPORT = re.compile(r"^from \.routes\.(\w+) import", re.MULTILINE)


def _git_tracked(path: Path) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def _in_git_repo() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def test_every_route_module_imported_by_app_is_tracked():
    """An untracked route module imports fine locally but breaks every fresh clone.

    This is the #57 failure: app.py imported mcp_routes, which was never committed,
    so cloning main and starting the server raised ModuleNotFoundError.
    """
    if not _in_git_repo():
        pytest.skip("not a git checkout")

    modules = _ROUTE_IMPORT.findall(APP_PY.read_text(encoding="utf-8"))
    assert modules, "no route imports found in app.py — has the import style changed?"

    untracked = [m for m in modules if not _git_tracked(ROUTES_DIR / f"{m}.py")]
    assert not untracked, (
        "app.py imports route modules that git does not track: "
        + ", ".join(f"cleanplex/web/routes/{m}.py" for m in untracked)
        + " -- commit them or a fresh clone will fail to start"
    )
