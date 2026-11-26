"""mu_spec.dash.stamps: generations, observer forwarding and the token functions (S-139).

T-286 pins that every install raises its key's generation, that installed never
reaches the observer and that committed forwards the record key; T-287 pins the
exact token formula every page compares.
"""

import hashlib
import json

import pytest


class RecordingObserver:
    """A StampObserver that lists every call it receives."""

    def __init__(self) -> None:
        self.changed_keys: list[str] = []
        self.appeared_records: list[tuple[object, str]] = []
        self.appended_records: list[object] = []

    def changed(self, key: str) -> None:
        self.changed_keys.append(key)

    def appeared(self, record, how: str) -> None:
        self.appeared_records.append((record, how))

    def appended(self, record) -> None:
        self.appended_records.append(record)


def test_generation_and_forwarding():
    from mu_spec.dash.stamps import (
        ALL_KEY,
        INBOX_KEY,
        Stamp,
        Stamps,
        marker_key,
        project_key,
    )

    s = Stamps()
    rec = RecordingObserver()

    # 1. the first install of a key is generation 0, even unnotified
    s.installed("p", b"a", notify=False)
    assert s.get(project_key("p")) == Stamp(b"a", hashlib.sha256(b"a").hexdigest(), 0)
    assert s.generation("p") == 0
    digest = s.get(project_key("p")).digest
    assert len(digest) == 64
    assert digest == digest.lower()

    # 2. identical bytes still raise the generation
    s.installed("p", b"a", notify=True)
    assert s.generation("p") == 1
    assert s.get(project_key("p")).digest == digest

    # 3. new bytes raise it again and change the digest
    s.installed("p", b"b", notify=True)
    assert s.generation("p") == 2
    assert s.get(project_key("p")).digest == hashlib.sha256(b"b").hexdigest()

    # 4. the inbox is kept under its own key; an unstamped project has no generation
    s.installed(None, b"{}", notify=False)
    assert s.get(INBOX_KEY).generation == 0
    assert s.generation("q") is None

    # 5. committed with no observer set is silent
    s.committed("p")

    # 6. installed never forwards; committed forwards the record key
    s.observe(rec)
    s.installed("p", b"c", notify=True)
    assert rec.changed_keys == []
    s.committed("p")
    s.committed(None)
    assert rec.changed_keys == ["project/p", "inbox"]

    # 7. one observer only
    with pytest.raises(RuntimeError):
        s.observe(rec)

    # 8. read answers every key, None for an unstamped one
    assert s.read([project_key("p"), INBOX_KEY, project_key("zz")]) == {
        "project/p": s.get("project/p"),
        "inbox": s.get("inbox"),
        "project/zz": None,
    }

    # 9. the marker's observer names map onto record keys
    assert marker_key("p") == "project/p"
    assert marker_key(None) == ALL_KEY == "*"
    assert project_key("inbox") != INBOX_KEY


def test_token_functions():
    from mu_spec.dash.stamps import (
        TOKEN_VERSION,
        Stamps,
        canonical,
        list_token,
        project_key,
        record_digest,
        screen_token,
    )
    from mu_spec.order import names_sorted

    d1 = record_digest(b"x")
    d2 = record_digest(b"y")

    # 1. non-ASCII kept raw, no spaces
    assert canonical(["é"]) == '["é"]'.encode()
    assert len(canonical(["é"])) == 6
    assert canonical({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'

    # 2. the exact formula, computed here
    assert TOKEN_VERSION == "dash-1"
    expected = hashlib.sha256(
        json.dumps([TOKEN_VERSION, "spine", [d1, d2], None], ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    assert screen_token("spine", [d1, d2], None) == expected

    # 3. the marker changes the token
    with_n1 = screen_token("ship", [d1], ("n", 1, None))
    with_n2 = screen_token("ship", [d1], ("n", 2, None))
    without = screen_token("ship", [d1], None)
    assert with_n1 != with_n2
    assert with_n2 != without
    assert with_n1 != without

    # 4. digest order and view name are part of the token
    assert screen_token("spine", [d1, d2], None) != screen_token("spine", [d2, d1], None)
    assert screen_token("spine", [d1], None) != screen_token("order", [d1], None)

    # 5. list order does not change the list token
    assert list_token(["q", "p"]) == list_token(["p", "q"])
    assert list_token(["p", "q"]) == record_digest(canonical([TOKEN_VERSION, "list", names_sorted(["p", "q"])]))

    # 6. a restart over the same bytes gives the same digests and so the same tokens
    installs = [("p", b'{"name":"p"}'), (None, b"{}"), ("p", "{\"t\":\"é\"}".encode())]
    first, second = Stamps(), Stamps()
    for project, data in installs:
        first.installed(project, data, notify=False)
        second.installed(project, data, notify=False)
    keys = [project_key("p"), "inbox"]
    first_digests = [first.get(k).digest for k in keys]
    second_digests = [second.get(k).digest for k in keys]
    assert first_digests == second_digests
    assert screen_token("spine", first_digests, None) == screen_token("spine", second_digests, None)
