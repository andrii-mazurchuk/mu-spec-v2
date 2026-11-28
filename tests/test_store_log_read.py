"""read_log over whole lines only, and the next seq after the last whole line (T-414).

A plain unit test: no acceptance input, no unit started, no running-unit bytes. The project record
and the log lines are built in process by the rebuilt codec. Modules under test are imported inside
the test, so a module not yet written fails only the tests that need it.
"""

import datetime
import os

AT = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=datetime.UTC)


def test_read_log_and_numbering(tmp_path):
    from mu_spec.codec import (
        EVENT_KINDS,
        MUTATION_KIND,
        ProjectRecord,
        encode_log_line,
        encode_record,
        log_path,
        new_log_record,
        record_path,
        with_seq,
    )
    from mu_spec.store import SEQ_START, Store

    from mu_spec.outside import load_config

    others = sorted(k for k in EVENT_KINDS if k != MUTATION_KIND)
    assert others, "EVENT_KINDS holds no kind besides MUTATION_KIND"
    k2 = others[0]
    b = SEQ_START + 100

    def rec(kind, project):
        return new_log_record(kind, project, [], {"n": 1}, AT)

    l0 = encode_log_line(with_seq(rec(MUTATION_KIND, "p"), b))
    l1 = encode_log_line(with_seq(rec(k2, "p"), b + 1))
    assert l1.endswith(b"\n")
    l1 = l1[:-1] + b"\r\n"
    l2 = encode_log_line(with_seq(rec(MUTATION_KIND, "q"), b + 2))
    torn = encode_log_line(with_seq(rec(MUTATION_KIND, "p"), b + 100))[:-1]
    D = tmp_path / "state"
    c = load_config({"HOLONIC_STATE_DIR": str(D), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    R = c.root
    assert R == D / "projects"
    record_path(R, "p").parent.mkdir(parents=True)
    record_path(R, "p").write_bytes(encode_record(ProjectRecord("p", {"name": "p", "entries": [], "counters": {}})))
    log_path(R).write_bytes(l0 + l1 + l2 + torn)

    s = Store(c)

    # 1. whole lines only
    assert [r.seq for r in s.read_log()] == [b, b + 1, b + 2]
    # 2. filters
    assert [r.seq for r in s.read_log(kind=MUTATION_KIND)] == [b, b + 2]
    assert [r.seq for r in s.read_log(project="p")] == [b, b + 1]
    assert [r.seq for r in s.read_log(project="p", kind=MUTATION_KIND)] == [b]
    assert s.read_log(project="zzz") == []
    # 3. the CR LF line read whole
    assert s.read_log()[1].kind == k2
    # 4. numbered after the last whole line
    appended = s.append_log(rec(MUTATION_KIND, "p"))
    assert appended.seq == b + 3
    assert log_path(R).read_bytes().endswith(encode_log_line(appended))

    # 5. an absent state directory numbers from SEQ_START, the log under the store root
    F = tmp_path / "fresh"
    cF = load_config({"HOLONIC_STATE_DIR": str(F), "MU_SPEC_PORT": "9100"}, tmp_path)[0]
    s2 = Store(cF)
    a = s2.append_log(rec(MUTATION_KIND, None))
    a2 = s2.append_log(rec(k2, None))
    assert a.seq == SEQ_START and a2.seq == SEQ_START + 1
    assert [r.seq for r in s2.read_log()] == [SEQ_START, SEQ_START + 1]
    assert log_path(cF.root).is_file()
    assert os.listdir(F) == ["projects"]
