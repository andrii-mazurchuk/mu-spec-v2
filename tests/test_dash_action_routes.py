"""The page's action routes against the unit's declared actions (T-183).

The page's names and routes are read from dashboard/src/actions.ts (S-219) by running bun from the
page source root; the unit's are mu_spec.declarations.ACTIONS (S-319) and
mu_spec.dash.settle.WAITING_ACTIONS (S-136), imported inside the test.
"""

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "dashboard"
ACTIONS_TS = DASHBOARD / "src" / "actions.ts"


def read_page_routes() -> dict:
    assert ACTIONS_TS.is_file(), "dashboard/src/actions.ts is absent"
    script = (
        f"const {{ ACTION_NAMES, ACTION_ROUTES }} = await import({json.dumps(ACTIONS_TS.as_uri())});"
        "const routes = {};"
        "for (const a of ACTION_NAMES) routes[a] = { method: ACTION_ROUTES[a].method, path: ACTION_ROUTES[a].path('zz9') };"
        "console.log(JSON.stringify({ names: ACTION_NAMES, routes }));"
    )
    done = subprocess.run(
        ["bun", "-e", script], check=False, cwd=DASHBOARD, capture_output=True, text=True, encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_page_routes_equal_declared_actions():
    from mu_spec.dash.settle import WAITING_ACTIONS
    from mu_spec.declarations import ACTIONS

    page = read_page_routes()
    names, routes = page["names"], page["routes"]
    declared = {spec.name: spec for spec in ACTIONS}

    assert set(names) == set(WAITING_ACTIONS)
    for a in names:
        spec = declared[a]
        assert routes[a]["method"] == spec.method, a
        assert routes[a]["path"] == spec.path.replace("{project}", "zz9").removeprefix("/"), a

    assert [spec.name for spec in ACTIONS if spec.name not in names] == ["rollback_tickets"]
