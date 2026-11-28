"""The lifecycle log's framing in mu_spec.codec: record fields, numbering, encoding and splitting (S-439).

A plain unit test over literal records: it reads no environment variable and starts no unit. The
module under test is imported inside the test, so its absence fails only this test.
"""

import datetime

import pytest

# U+0085, U+2028 and U+2029: line boundaries to str.splitlines, never to the log's framing
NEL, LS, PS = "\u0085", " ", " "
RAW_SEPARATORS = (b"\xc2\x85", b"\xe2\x80\xa8", b"\xe2\x80\xa9")


def test_log_framing():
    from mu_spec.codec import (
        EVENT_KINDS,
        LOG_FIELDS,
        MUTATION_KIND,
        decode_log_line,
        encode_log_line,
        format_time,
        new_log_record,
        split_log,
        thaw_value,
        with_seq,
    )

    at = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=datetime.timezone.utc)  # noqa: UP017 -- the arrange T-112 writes
    facts = {"t": f"a{NEL}b{LS}c{PS}d\ne"}
    r = new_log_record(MUTATION_KIND, "p", [], facts, at)
    others = sorted(k for k in EVENT_KINDS if k != MUTATION_KIND)
    assert others, "EVENT_KINDS holds no kind other than MUTATION_KIND"
    k2 = others[0]

    # 1. field order without seq, and the record's accessors
    assert list(r.doc) == ["at", "project", "kind", "refs", "facts"]
    assert r.doc["at"] == format_time(at)
    assert r.seq is None
    assert r.kind == MUTATION_KIND
    assert r.project == "p"

    # 2. a record with no seq is never encoded
    with pytest.raises(ValueError):
        encode_log_line(r)

    # 3. the kind vocabulary is closed
    assert "no-such-kind" not in EVENT_KINDS
    with pytest.raises(ValueError):
        new_log_record("no-such-kind", "p", [], {}, at)

    # 4. with_seq gives a new record in LOG_FIELDS order and leaves r alone
    r7 = with_seq(r, 7)
    assert list(r7.doc) == list(LOG_FIELDS)
    assert r7.seq == 7
    assert "seq" not in r.doc

    # 5. one terminator per line, the three separators kept as raw UTF-8
    line1 = encode_log_line(r7)
    line2 = encode_log_line(with_seq(new_log_record(k2, None, [], facts, at), 8))
    for line in (line1, line2):
        assert line.endswith(b"\n")
        assert line.count(b"\n") == 1
        for raw in RAW_SEPARATORS:
            assert raw in line, f"{raw!r} not written raw in {line!r}"

    # 6. split on b'\n' only; each line decodes back to its record
    assert split_log(line1 + line2) == ((line1[:-1], line2[:-1]), b"")
    assert thaw_value(decode_log_line(line1[:-1]).doc) == thaw_value(r7.doc)
    d2 = decode_log_line(line2[:-1])
    assert d2.kind == k2
    assert d2.project is None

    # 7. a CR before the LF is dropped; the torn tail stays apart
    assert split_log(b"A\nB\r\nC") == ((b"A", b"B"), b"C")
    assert split_log(b"") == ((), b"")
    assert split_log(b"A\n") == ((b"A",), b"")
