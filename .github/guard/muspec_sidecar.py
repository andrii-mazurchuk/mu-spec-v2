#!/usr/bin/env python3
"""Parse the `## Machine-readable` sidecar out of a mu-spec issue body.

The single place this rule lives. The ingest needs it to mirror units; the
path-guard needs it to know a unit's write set. Two copies of one rule is the
defect that cost mu-spec an afternoon, so there is one copy and both vendor it.

Self-check: python3 muspec_sidecar.py --demo
"""

import json
import re

# mu-spec exports MACHINE_HEADING / MACHINE_FENCE from mu_spec.render. We are a
# different process on a different host, so we match the fence rather than
# import it. The heading may be reworded; the field names are the contract.
_FENCE = re.compile(r"```json\n(.*?)\n```", re.S)

MIDDLE_DOT = "·"

REQUIRED = ("key", "anchor", "kind", "repo", "cut_seq", "write_set",
            "blocked_by", "mutex", "file_scope")

# git refuses these in a ref name. Keys are branch names verbatim, so a key
# carrying one is unusable rather than merely ugly.
_REF_ILLEGAL = set(":~^?*[\\ \t")


class Malformed(Exception):
    """The body is not a mu-spec unit we will run.

    Always fatal. A unit with no parseable write_set has no path-guard, and a
    unit we cannot guard is one we do not execute.
    """


def parse(body):
    """Return the sidecar dict from an issue body. Raise Malformed otherwise."""
    blocks = _FENCE.findall(body)
    if not blocks:
        raise Malformed("no ```json fence: not a mu-spec unit, or emitted "
                        "before the sidecar shipped. Never fall back to prose.")

    # LAST fence, not first. Contract bodies are human prose and carry their own
    # fenced code -- both real samples open with a ```python block inside
    # `## Contract`, so a first-fence parser returns Python source and *looks*
    # like it worked.
    try:
        d = json.loads(blocks[-1])
    except ValueError as e:
        raise Malformed("last ```json fence is not valid JSON: %s" % e) from e

    missing = [f for f in REQUIRED if f not in d]
    if missing:
        raise Malformed("sidecar missing %s" % ", ".join(missing))
    if d["kind"] not in ("implementation", "test"):
        raise Malformed("unknown kind %r" % d["kind"])
    if not d["write_set"]:
        raise Malformed("empty write_set: nothing to guard")

    # Reject the pre-2026-09-29 spelling rather than normalising it. mu-spec
    # accepts both on input forever because its history holds the old form; we
    # have no history, so a middle-dot key came from something that is not the
    # producer. Normalising it quietly would tolerate output from an unknown
    # source -- the same hole the author gate exists to close.
    #
    # Scoped to keys. NOT the body: contract prose legitimately contains U+00B7
    # as multiplication (`m,k`, `chi2_{m,k}(a)/m`) and dark is a thesis full of
    # it. Scanning the prose fires on correct maths.
    for field in ("key", "anchor"):
        if MIDDLE_DOT in d[field]:
            raise Malformed("%s %r carries U+00B7: not from mu-spec" % (field, d[field]))
    for field in ("blocked_by", "mutex"):
        bad = [k for k in d[field] if MIDDLE_DOT in k]
        if bad:
            raise Malformed("%s carries U+00B7 keys: %s" % (field, bad))

    return d


def branch_name(key):
    """The branch for a unit. It is the key, verbatim.

    No transform exists on either side, which is the point: nothing can
    disagree about which unit a branch belongs to. This function only asserts
    that -- if it ever needs to *change* the key, the standard has regressed.
    """
    if not key.isascii():
        raise Malformed("key %r is not ASCII" % key)
    bad = sorted(_REF_ILLEGAL & set(key))
    if bad or ".." in key or key.endswith(".lock"):
        raise Malformed("key %r is not a legal git ref (%s)" % (key, bad or ".."))
    return key


def outside_write_set(changed_paths, write_set):
    """Paths in the diff that the unit was not permitted to touch."""
    allowed = set(write_set)
    return sorted(p for p in changed_paths if p and p not in allowed)


def _demo():
    good = """## Contract

Imports `Subspace` (S-41). The band is `chi2_{m·k}(α)/m` at `n·k` dof.

```python
def f(): return 1
```

## Machine-readable

```json
{"key": "S-76", "anchor": "S-76", "kind": "implementation",
 "repo": "andrii-mazurchuk/DARK", "cut_seq": 3, "slices": ["swarm-algorithms"],
 "write_set": ["dark/estimation/anchor.py"], "blocked_by": ["S-76-T"],
 "mutex": [], "file_scope": {"dark/estimation/anchor.py": ["S-30"]}}
```
"""
    d = parse(good)
    assert d["key"] == "S-76", d["key"]
    assert d["kind"] == "implementation"
    # the python fence must not have been mistaken for the sidecar
    assert "def f()" not in json.dumps(d)
    # legitimate multiplication dots in the prose must not trip anything
    assert good.count(MIDDLE_DOT) == 2

    assert branch_name("S-76-T") == "S-76-T"
    for bad_key in ("S·76", "S-76:T", "a..b", "x y"):
        try:
            branch_name(bad_key)
        except Malformed:
            pass
        else:
            raise AssertionError("branch_name accepted %r" % bad_key)

    assert outside_write_set(["a.py", "tests/t.py"], ["a.py"]) == ["tests/t.py"]
    assert outside_write_set(["a.py"], ["a.py", "b.py"]) == []

    for body, why in (
        ("no fence at all", "missing fence"),
        ("```json\nnot json\n```", "bad json"),
        ('```json\n{"key": "S-1"}\n```', "missing fields"),
    ):
        try:
            parse(body)
        except Malformed:
            pass
        else:
            raise AssertionError("parse accepted %s" % why)

    # a first-fence parser would return the python block; prove ours does not
    two = good.replace('{"key": "S-76"', '{"key": "S-76"')
    assert parse(two)["key"] == "S-76"

    # middle-dot key inside an otherwise valid block
    dotted = good.replace('"key": "S-76"', '"key": "S·76"')
    try:
        parse(dotted)
    except Malformed as e:
        assert "U+00B7" in str(e)
    else:
        raise AssertionError("parse accepted a middle-dot key")

    print("muspec_sidecar: all checks pass")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        _demo()
    else:
        print(json.dumps(parse(sys.stdin.read()), indent=2))
