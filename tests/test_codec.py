"""mu_spec.codec on fixed fixtures: record and temp-copy paths, deep immutability and copying (S-439).

Plain unit tests: they read no environment variable, start no unit and use no acceptance copy. The
round trip over stored bytes is held by the in-process acceptance checks; here the names and
documents are literals. The module under test is imported inside each test, so its absence fails
only the tests that need it.
"""

NAMES = ["plain", ".odd.tmp", "with space", "S-387 (was S-41)", "é-proj", ".hidden", "manifest"]
PENDING_MANIFEST = "manifest.pending.json"


def refuses(change) -> bool:
    """True when change() raises TypeError or AttributeError, the refusal of a frozen value."""
    try:
        change()
    except (TypeError, AttributeError):
        return True
    return False


def test_paths_and_temp_names(tmp_path):
    from mu_spec.codec import (
        TEMP_PREFIX,
        TEMP_SUFFIX,
        inbox_path,
        log_path,
        project_name_of,
        record_path,
        temp_path,
    )

    d = tmp_path / "store"
    assert TEMP_PREFIX == "."
    assert TEMP_SUFFIX == ".tmp"
    for p in NAMES:
        rp = record_path(d, p)
        # 1. the inverse, a project named like a temp copy included
        assert project_name_of(rp) == p, p
        # 2. the temp copy sits beside the record, under a fixed pattern
        t = temp_path(rp)
        assert t.parent == rp.parent, p
        assert t.name == TEMP_PREFIX + rp.name + TEMP_SUFFIX, p
        # 3. a temp copy is never a project record
        assert project_name_of(t) is None, p
        # 4. one fixed name per final path
        assert temp_path(record_path(d, p)) == temp_path(record_path(d, p)), p
        # 5. never the record itself nor the running unit's pending manifest
        assert t.name != rp.name, p
        assert t.name != PENDING_MANIFEST, p
        # 6. the record lies in the project's own directory directly under the store root
        assert rp.parent.parent == d, p
        assert rp.parent.name == p, p

    assert inbox_path(d).parent == d
    assert log_path(d).parent == d
    ti = temp_path(inbox_path(d))
    assert ti.parent == d
    assert project_name_of(inbox_path(d)) is None
    assert project_name_of(ti) is None
    assert project_name_of(log_path(d)) is None


def test_decoded_doc_is_deeply_frozen():
    from mu_spec.codec import (
        LogRecord,
        ProjectRecord,
        decode_inbox,
        decode_record,
        encode_inbox,
        encode_record,
    )

    top_doc = {"name": "p", "entries": [{"id": "I-28 (was I-01)", "title": "a", "tags": ["x"]}], "counters": {"I": 1}}
    b = encode_record(ProjectRecord("p", top_doc))
    r = decode_record("p", b)
    top = r.doc
    arr = top["entries"]
    inner = arr[0]
    inbox_doc = {"requests": [{"id": "msg-0001", "type": "initiate", "body": "b"}]}
    ib = encode_inbox(inbox_doc)
    q = decode_inbox(ib)

    # 1. the top-level object
    assert refuses(lambda: top.__setitem__("new", 1))
    assert refuses(lambda: top.__delitem__("name"))
    # 2. an array
    assert refuses(lambda: arr.append(1))
    # 3. an object inside an array, and its own array
    assert refuses(lambda: inner.__setitem__("title", "x"))
    assert refuses(lambda: inner.__delitem__("id"))
    assert isinstance(inner["tags"], tuple)
    # 4. nothing changed
    assert encode_record(r) == b

    # 5. thaw is a deep, mutable copy in stored key order
    t = r.thaw()
    t["new"] = 1
    t["entries"].append(1)
    t["entries"][0]["title"] = "x"
    assert encode_record(r) == b
    assert isinstance(t, dict)
    assert isinstance(t["entries"], list)
    assert list(t) == ["name", "entries", "counters", "new"]

    # 6. a second thaw sees none of it
    t2 = r.thaw()
    assert "new" not in t2
    assert len(t2["entries"]) == 1
    assert t2["entries"][0]["title"] == "a"

    # 7. the decoded inbox is frozen as deeply
    assert refuses(lambda: q.__setitem__("new", 1))
    assert refuses(lambda: q["requests"][0].__setitem__("body", "z"))
    assert encode_inbox(q) == ib

    # 8. constructors keep doc as given; from_doc freezes a copy
    d = {"a": [1]}
    assert ProjectRecord("p", d).doc is d
    assert LogRecord(d).doc is d
    f = ProjectRecord.from_doc("p", d).doc
    assert f is not d
    assert refuses(lambda: f.__setitem__("b", 1))
    assert isinstance(f["a"], tuple)
    assert f["a"] == (1,)
    assert f["a"] is not d["a"]
    assert d == {"a": [1]}
    assert isinstance(d, dict)
    assert isinstance(d["a"], list)
