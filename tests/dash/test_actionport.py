"""The action port's drawn controls and its imports (T-177).

DRAWN_CONTROLS is read from dashboard/src/actionport.ts (S-235) by running bun over it; the unit's
action names are mu_spec.declarations.ACTIONS (S-319), imported inside the test. The module's
import statements are read from its source text, comments removed.
"""

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "dashboard"
ACTIONPORT_TS = DASHBOARD / "src" / "actionport.ts"


def read_drawn_controls() -> dict:
    assert ACTIONPORT_TS.is_file(), "dashboard/src/actionport.ts is absent"
    script = (
        f"const {{ DRAWN_CONTROLS }} = await import({json.dumps(ACTIONPORT_TS.as_uri())});"
        "console.log(JSON.stringify(DRAWN_CONTROLS));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=DASHBOARD, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def code_of(text: str) -> str:
    """The source less its block and line comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_drawn_controls_and_imports():
    d = read_drawn_controls()

    from mu_spec.declarations import ACTIONS

    a = {spec.name for spec in ACTIONS}

    # 1. The views and the controls each draws, in order.
    assert set(d) == {"queues", "order", "ship"}
    assert d["queues"] == ["ratify", "reject"]
    assert d["order"] == ["cut_units"]
    assert d["ship"] == ["cut_units", "set_repo", "set_labels", "set_delegation", "emit_tickets"]

    # 2. Seven person-only actions, rollback absent; cut_units on two views, every other on one.
    names = [name for view in d.values() for name in view]
    assert len(names) == 8
    assert len(set(names)) == 7
    assert set(names) == a - {"rollback_tickets"}
    views = Counter(name for view in d.values() for name in set(view))
    assert views["cut_units"] == 2
    assert all(n == 1 for name, n in views.items() if name != "cut_units"), views

    # 3. One import statement, from ./net, naming only request and NetResult.
    code = code_of(ACTIONPORT_TS.read_text(encoding="utf-8"))
    assert len(re.findall(r"\bimport\b", code)) == 1, "actionport.ts must hold exactly one import"
    assert not re.search(r"\bexport\s*(?:type\s*)?(?:\*|\{[^}]*\})\s*from\b", code), "actionport.ts re-exports"
    assert not re.search(r"\brequire\s*\(", code), "actionport.ts requires a module"
    m = re.search(r"\bimport\s+(?:type\s+)?\{([^}]*)\}\s*from\s*[\"']([^\"']+)[\"']", code)
    assert m, "the import of actionport.ts is not a named import"
    assert m.group(2) == "./net"
    named = {re.sub(r"^type\s+", "", part.strip()) for part in m.group(1).split(",") if part.strip()}
    assert named == {"request", "NetResult"}
