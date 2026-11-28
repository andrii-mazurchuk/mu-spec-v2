"""mu_spec.order: id_key reads the numeric tail as a whole number; by_id and names_sorted order by it (T-28).

Modules under test are imported inside the test, so a module not yet written fails only the tests
that need it.
"""


def test_id_key_by_id_names_sorted():
    from mu_spec.order import by_id, id_key, names_sorted

    # 1. the tail after the last '-' as a base-10 int; no such tail -> -1
    assert id_key("S-387 (was S-41)") == ("S", 41, "S-387 (was S-41)")
    assert id_key("iss-1764") == ("iss", 1764, "iss-1764")
    assert id_key("msg-0145") == ("msg", 145, "msg-0145")
    assert id_key("plain") == ("plain", -1, "plain")

    # 2. the whole number orders, not its text
    assert id_key("S-500 (was S-09)") < id_key("S-387 (was S-41)")

    # 3. and 4. by_id sorts by id_key, never by the identifier text
    assert by_id(["iss-10", "iss-9"], ident=lambda s: s) == ["iss-9", "iss-10"]
    assert by_id(["S-400 (was S-100)", "S-500 (was S-09)", "S-10"], ident=lambda s: s) == [
        "S-500 (was S-09)",
        "S-10",
        "S-400 (was S-100)",
    ]

    # 5. code point order: 'B' is 66, 'a' 97, 'b' 98
    assert names_sorted(["b", "B", "a"]) == ["B", "a", "b"]
