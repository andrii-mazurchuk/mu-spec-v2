"""The codec round trip in process: stored bytes decoded and re-encoded give the same bytes (S-439, S-508).

In-process checks over the stored copy: the rebuilt mu_spec.codec runs directly over a working copy
of the staged acceptance store, and over written_store, the store the running unit wrote for the
captured calls. Every record is encoded from the frozen form as decoded and from the thawed form, so
a defect in handling either form is told apart from a codec defect. Nothing here launches a unit,
reads the environment or writes a stored file; a file that fails is named, never rewritten. The
codec is imported inside each test, so its absence fails only these tests.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

E_FORMS = {
    "e1": "encode_record(decode_record(name, b))",
    "e2": "encode_record(ProjectRecord.from_doc(name, r.thaw()))",
    "e3": "encode_log_line(decode_log_line(l))",
    "e4": "encode_record(ProjectRecord(name, r.thaw()))",
    "e5": "encode_log_line(LogRecord(thaw_value(d.doc)))",
    "e6": "encode_inbox(decode_inbox(ib))",
    "e7": "encode_inbox(thaw_value(decode_inbox(ib)))",
}
E_ACUTE_RAW = "é".encode()
E_ACUTE_ESCAPED = b"\\u00e9"


def snapshot(root: Path) -> dict[str, bytes]:
    """{path relative to root, '/' separators: bytes} of every file under root."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def walk(value: object):
    """Every value at any depth of a thawed value, value itself first."""
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def strings(value: object) -> list[str]:
    return [v for v in walk(value) if isinstance(v, str)]


def objects(value: object) -> list[Mapping]:
    return [v for v in walk(value) if isinstance(v, Mapping)]


def is_plain(value: object) -> bool:
    """True when value is a dict holding only dicts, lists and scalars at every depth."""
    return isinstance(value, dict) and not any(isinstance(v, (tuple, Mapping)) and not isinstance(v, dict) for v in walk(value))


def unsorted_count(value: object) -> int:
    return sum(1 for o in objects(value) if list(o) != sorted(o))


def project_names(codec, store: Path) -> list[str]:
    """Every directory directly under store holding a file at record_path(store, its name)."""
    return sorted(p.name for p in store.iterdir() if p.is_dir() and codec.record_path(store, p.name).is_file())


def round_trip(codec, store: Path) -> tuple[list[str], int, int]:
    """(failures naming file, line and form, number of decoded objects, those with unsorted keys)."""
    failures: list[str] = []
    decoded: list[object] = []

    def check(form: str, got: bytes, want: bytes, where: str) -> None:
        if got != want:
            failures.append(f"{where}: {form} = {E_FORMS[form]} differs from the stored bytes")

    for name in project_names(codec, store):
        path = codec.record_path(store, name)
        b = path.read_bytes()
        r = codec.decode_record(name, b)
        check("e1", codec.encode_record(r), b, str(path))
        check("e2", codec.encode_record(codec.ProjectRecord.from_doc(name, r.thaw())), b, str(path))
        t = r.thaw()
        direct = codec.ProjectRecord(name, t)
        assert direct.doc is t, f"{path}: ProjectRecord(name, t).doc is not the thawed dict passed in"
        assert is_plain(t), f"{path}: r.thaw() is not a plain dict holding lists"
        check("e4", codec.encode_record(direct), b, str(path))
        decoded.append(t)

    inbox = codec.inbox_path(store)
    if inbox.is_file():
        ib = inbox.read_bytes()
        check("e6", codec.encode_inbox(codec.decode_inbox(ib)), ib, str(inbox))
        plain = codec.thaw_value(codec.decode_inbox(ib))
        assert is_plain(plain), f"{inbox}: thaw_value(decode_inbox(ib)) is not a plain dict holding lists"
        check("e7", codec.encode_inbox(plain), ib, str(inbox))
        decoded.append(plain)

    log = codec.log_path(store)
    if log.is_file():
        lines, _tail = codec.split_log(log.read_bytes())
        for i, line in enumerate(lines, 1):
            where = f"{log} line {i}"
            d = codec.decode_log_line(line)
            check("e3", codec.encode_log_line(d), line + b"\n", where)
            plain = codec.thaw_value(d.doc)
            record = codec.LogRecord(plain)
            assert record.doc is plain, f"{where}: LogRecord(doc).doc is not the dict passed in"
            assert is_plain(plain), f"{where}: thaw_value(d.doc) is not a plain dict holding lists"
            check("e5", codec.encode_log_line(record), line + b"\n", where)
            if list(d.doc) != list(codec.LOG_FIELDS):
                failures.append(f"{where}: keys {list(d.doc)} are not in LOG_FIELDS order {list(codec.LOG_FIELDS)}")
            if d.kind not in codec.EVENT_KINDS:
                failures.append(f"{where}: kind {d.kind!r} is not in EVENT_KINDS")
            decoded.append(plain)

    return failures, len(decoded), sum(unsorted_count(v) for v in decoded)


def test_staged_store_round_trips(acceptance_inputs, tmp_path):
    from mu_spec import codec

    work = tmp_path / "work"
    shutil.copytree(acceptance_inputs.staged_store, work)
    store = work / "projects"
    assert project_names(codec, store), f"{store} holds no project: the staged acceptance store is empty"
    log = codec.log_path(store)
    if log.is_file():
        assert b"\r" not in log.read_bytes(), f"{log} holds a CR byte: the live store's log is LF-ended"
    snap0 = snapshot(work)

    failures, count, unsorted = round_trip(codec, store)

    print(f"staged store: {unsorted} decoded objects have keys not in sorted order, across {count} documents")
    assert not failures, "\n".join(failures)
    assert snapshot(work) == snap0, "the codec wrote under the working copy"


def test_written_store_round_trips(written_store):
    from mu_spec import codec
    from tests.acceptance.inprocess.conftest import WRITTEN_BODY, WRITTEN_PROJECT

    store = Path(written_store)
    snap0 = snapshot(store)
    capture = f"the capture of the running unit's store for {WRITTEN_PROJECT!r}"
    x_path = codec.record_path(store, WRITTEN_PROJECT)

    # 1. the fixture project's record holds the body as the running unit stored it
    assert x_path.is_file(), f"{x_path} is not a file: {capture} holds no record"
    b_x = x_path.read_bytes()
    x_doc = codec.decode_record(WRITTEN_PROJECT, b_x).thaw()
    assert WRITTEN_BODY in strings(x_doc), f"{x_path}: {capture} holds no string equal to WRITTEN_BODY"
    # 2. a stored key order that is not sorted
    assert any(list(o) != sorted(o) for o in objects(x_doc)), f"{x_path}: {capture} holds no object with unsorted keys"
    # 3. one form for the e-acute
    if E_ACUTE_ESCAPED in b_x and E_ACUTE_RAW not in b_x:
        print(f"written store: the running unit escapes non-ASCII text ({E_ACUTE_ESCAPED!r})")
    elif E_ACUTE_RAW in b_x and E_ACUTE_ESCAPED not in b_x:
        print(f"written store: the running unit writes non-ASCII text raw ({E_ACUTE_RAW!r})")
    else:
        raise AssertionError(f"{x_path}: {capture} holds the e-acute in neither form alone, or in both")
    # 4. the inbox and a log with a whole line
    inbox, log = codec.inbox_path(store), codec.log_path(store)
    assert inbox.is_file(), f"{inbox} is not a file: {capture} holds no inbox"
    assert log.is_file(), f"{log} is not a file: {capture} holds no lifecycle log"
    assert codec.split_log(log.read_bytes())[0], f"{log}: {capture} holds no whole log line"
    # 5. the node's LF-only form
    with_cr = [rel for rel, data in snap0.items() if b"\r" in data]
    assert not with_cr, f"{capture} holds a CR byte in {with_cr}"
    assert WRITTEN_PROJECT in project_names(codec, store), f"{capture}: {WRITTEN_PROJECT!r} is not listed as a project"

    failures, _count, _unsorted = round_trip(codec, store)

    assert not failures, "\n".join(failures)
    assert snapshot(store) == snap0, "the codec wrote under the written store"
