"""unitcheck.masks: the one mask the events, recording-failure and independence checks apply, the
varying-path walk of the two-run declaration check, and the content key the drop count compares.

The modules under test are imported inside each test, so while one is absent only these tests fail.
"""


def test_varying_and_declared():
    from unitcheck.masks import STAMPED_FIELDS, declared, varying

    assert varying({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 3], "c": 0}) == [("b", 1), ("c",)]
    # A length difference is one path, at the shorter list's end.
    assert varying([1], [1, 2, 3]) == [(1,)]
    assert varying({"x": {"y": "p"}}, {"x": {"y": "p"}}) == []

    assert declared(("x", "at"), 1, 2) is True
    assert declared(("x", "seq"), 1, 2) is False
    # Only the last str element of the path counts, not any element.
    assert declared(("at", "x"), 1, 2) is False
    assert declared(("at", 3), 1, 2) is True

    # Two strs are declared only when both are run ids.
    assert declared(("n",), "9f2c4e81a07b", "0a1b2c3d4e5f") is True
    assert declared(("n",), "9f2c4e81a07b", "zz") is False

    for f in STAMPED_FIELDS:
        assert declared(("r", f), 1.0, 2.0) is True, f
    assert declared(("r", "run_id"), "a", "b") is True


def test_content_key():
    from unitcheck.masks import content_key

    base = {
        "seq": 7,
        "at": 1764100000.5,
        "kind": "tickets_emitted",
        "refs": ["S-17 (was S-12)"],
        "facts": {"run_id": "9f2c4e81a07b"},
    }

    assert content_key(base) == content_key(dict(base, seq=8, at=1764100009.0))
    assert content_key(base) == content_key(dict(base, facts={"run_id": "0a1b2c3d4e5f"}))
    # A run id held as a list element is masked too.
    assert content_key({"kind": "k", "refs": ["9f2c4e81a07b"], "facts": {}}) == content_key(
        {"kind": "k", "refs": ["0a1b2c3d4e5f"], "facts": {}}
    )
    assert content_key(base) != content_key(dict(base, refs=["S-13"]))
    # 13 characters, not run ids.
    assert content_key({"kind": "k", "refs": [], "facts": {"x": "9f2c4e81a07b0"}}) != content_key(
        {"kind": "k", "refs": [], "facts": {"x": "0a1b2c3d4e5f0"}}
    )
    # Only the top-level seq is dropped.
    assert content_key({"facts": {"seq": 1}}) != content_key({"facts": {"seq": 2}})
    assert content_key({"kind": "é"}) == '{"kind":"é"}'.encode()


def test_mask_fields_and_bytes():
    from mu_spec.contract import TIME_FIELDS as S322_TIME_FIELDS
    from shipcheck.judge import TIME_FIELDS as S305_TIME_FIELDS
    from shipcheck.judge import TIME_MARK
    from unitcheck.masks import EXTRA_FIELDS, MASK_FIELDS, STAMPED_FIELDS, mask

    tm = TIME_MARK.encode("utf-8")

    assert len(set(MASK_FIELDS)) == len(MASK_FIELDS)
    assert MASK_FIELDS[:4] == ("started_at", "finished_at", "at", "computed_at")
    for f in STAMPED_FIELDS:
        assert f not in S305_TIME_FIELDS, f
        assert f not in S322_TIME_FIELDS, f
    assert EXTRA_FIELDS[0] == "computed_at"

    expected = b'{"computed_at": "' + tm + b'", "unit": "mu-spec"}'
    assert isinstance(expected, bytes)
    assert mask(b'{"computed_at": 1764100000.25, "unit": "mu-spec"}') == expected

    # Only the scalar after the key is replaced; the rest of the object stays.
    for f in STAMPED_FIELDS:
        fb = f.encode("utf-8")
        expected = b'{"x": {"' + fb + b'": "' + tm + b'"}, "y": 2}'
        assert isinstance(expected, bytes)
        assert mask(b'{"x": {"' + fb + b'": 1764100000.5}, "y": 2}') == expected, f

    # No MASK_FIELDS key and no run id: byte for byte, odd spacing kept.
    expected = b'{"a" :1,  "b":"x"}'
    assert isinstance(expected, bytes)
    assert mask(b'{"a" :1,  "b":"x"}') == expected

    # null is a scalar too.
    expected = b'{"computed_at":"' + tm + b'"}'
    assert isinstance(expected, bytes)
    assert mask(b'{"computed_at":null}') == expected
