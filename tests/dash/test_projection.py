"""mu_spec.dash.projection: published parts, the pick rule and Published (S-192).

T-134 pins the byte form part_json gives and that a pick keeps every item whose
keys hold it; T-135 pins that Published.wait_until compares the marker's nonce
before its counter, wakes on a publish and gives False at the deadline, and that
publish refuses an older projection.
"""

import json
import threading
import time

import pytest


def test_part_json_picks():
    from mu_spec.dash.projection import VIEWS, Item, Part, part_json

    part = Part(
        b'{"a":1}',
        (
            Item(frozenset({"x"}), b"1"),
            Item(frozenset({"y"}), b"2"),
            Item(frozenset({"x", "y"}), b"3"),
        ),
    )

    # 1. no pick keeps every item, in order
    everything = part_json(part, None)
    assert everything == b'{"fields":{"a":1},"items":[1,2,3]}'

    # 2-3. a pick keeps every item whose keys hold it, not only those equal to it
    picked_x = part_json(part, "x")
    assert picked_x == b'{"fields":{"a":1},"items":[1,3]}'
    picked_y = part_json(part, "y")
    assert picked_y == b'{"fields":{"a":1},"items":[2,3]}'

    # 4. a pick no item holds keeps none
    picked_z = part_json(part, "z")
    assert picked_z == b'{"fields":{"a":1},"items":[]}'

    # 5. an empty part, and every output is JSON
    empty = part_json(Part(b"{}", ()), None)
    assert empty == b'{"fields":{},"items":[]}'
    for out in (everything, picked_x, picked_y, picked_z, empty):
        json.loads(out)

    # 6. the views, in B-06's order
    assert VIEWS == ("spine", "order", "read", "waves", "modules", "queues", "ship")


def test_published_wait_and_order():
    from mu_spec.dash.projection import (
        VIEWS,
        Part,
        Projection,
        Published,
        ViewProjection,
    )

    def proj(gen, marker):
        return Projection(
            "p",
            gen,
            marker,
            {
                v: ViewProjection("t", Part(b"{}", ()), None, frozenset(), {})
                for v in VIEWS
            },
        )

    def past():
        return 10.0

    pub = Published()

    # 1. nothing held: no projection, and the wait gives False at the deadline
    assert pub.projection("p") is None
    assert pub.wait_until("p", 0, None, 5.0, past) is False

    # 2. the nonce is compared before the counter
    pub.publish(proj(4, ("m", 5, None)))
    assert pub.wait_until("p", 4, None, 5.0, past) is True
    assert pub.wait_until("p", 4, ("n", 3), 5.0, past) is False
    assert pub.wait_until("p", 5, None, 5.0, past) is False

    # 3. an equal generation with a new nonce is accepted
    pub.publish(proj(4, ("n", 3, None)))
    assert pub.wait_until("p", 4, ("n", 3), 5.0, past) is True
    assert pub.wait_until("p", 4, ("n", 4), 5.0, past) is False

    # 4. an older generation, or a lower counter of the same nonce, is refused
    with pytest.raises(ValueError):
        pub.publish(proj(3, ("n", 3, None)))
    assert pub.projection("p").generation == 4
    with pytest.raises(ValueError):
        pub.publish(proj(4, ("n", 2, None)))

    # 5. the wait blocks and wakes on a publish at the point
    t0 = time.monotonic()

    def later():
        time.sleep(0.1)
        pub.publish(proj(5, ("n", 3, None)))

    thread = threading.Thread(target=later)
    thread.start()
    try:
        assert pub.wait_until("p", 5, None, t0 + 5.0, time.monotonic) is True
        assert time.monotonic() - t0 < 2.0
    finally:
        thread.join()

    # 6. the held reference is returned, not a copy
    p = proj(6, ("n", 3, None))
    pub.publish(p)
    assert pub.projection("p") is p
