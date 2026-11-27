"""mu_spec.dash.views: the reads every screen is drawn from and how views carry them (S-142).

T-137 pins the 16 bundle reads, that every head and rest name is a bundle read and
together they draw all of them, that inbox and Ship reads are drawn only in views
whose coverage (S-247) holds them, and each view's server and local filters.
"""


def test_views_declarations_agree():
    from mu_spec.dash.projection import VIEWS
    from mu_spec.dash.snapshot import COVERAGE, INBOX, INBOX_VIEWS, MARKER
    from mu_spec.dash.views import (
        BUNDLE,
        DEFAULT_LAYER,
        HEAD,
        INBOX_READS,
        LAYERS,
        LOCAL_FILTERS,
        MARKER_READS,
        REST,
        SERVER_FILTER,
    )

    # 1. Sixteen reads, none of them the project list, the two event reads by kind.
    assert len(BUNDLE) == 16
    assert all(tool != "list_projects" for tool, _ in BUNDLE.values())
    assert BUNDLE["emitted"] == ("list_events", {"kind": "tickets_emitted"})
    assert BUNDLE["ship_failed"] == ("list_events", {"kind": "ship_failed"})

    # 2. Every table is keyed by exactly the views.
    assert set(HEAD) == set(REST) == set(SERVER_FILTER) == set(LOCAL_FILTERS) == set(VIEWS)

    # 3. Heads and rests name only bundle reads and together draw all of them.
    drawn: set[str] = set()
    for v in VIEWS:
        for n in tuple(HEAD[v]) + tuple(REST[v]):
            assert n in BUNDLE, (v, n)
            drawn.add(n)
    assert drawn == set(BUNDLE)

    # 4. A read of the inbox or the run state is drawn only where coverage holds it.
    inbox_drawers = set()
    for v in VIEWS:
        for n in tuple(HEAD[v]) + tuple(REST[v]):
            if n in INBOX_READS:
                assert INBOX in COVERAGE[v], (v, n)
                inbox_drawers.add(v)
            if n in MARKER_READS:
                assert MARKER in COVERAGE[v], (v, n)
    assert inbox_drawers == set(INBOX_VIEWS)

    # 5. Server filters: spine, order and read only; read's is the layer.
    assert {v for v, f in SERVER_FILTER.items() if f is not None} == {"spine", "order", "read"}
    assert SERVER_FILTER["read"] == "layer"
    assert LAYERS == ("I", "B", "A", "S", "T")
    assert DEFAULT_LAYER in LAYERS

    # 6. A server filter is never also local; the layer read draws no bundle read.
    for v in VIEWS:
        assert SERVER_FILTER[v] not in LOCAL_FILTERS[v], v
    assert HEAD["read"] == ()
    assert REST["read"] == ()
