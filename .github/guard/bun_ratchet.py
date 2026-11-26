#!/usr/bin/env python3
"""Test ratchet for a Bun repo: a test passing on the base must still pass on the PR head.

main is red by design while it is built (test units land failing suites before the code), so
this is the PR gate, not "all green". Results come from bun's JUnit report.

    python3 bun_ratchet.py <base_dir> <head_dir> [redefined test files...]
    python3 bun_ratchet.py --demo

A test unit (branch S-...-T) may redefine the tests in the files it changed; those are passed as
`redefined` and exempt. This file is in no unit's write_set; path-guard protects it.
"""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET


def results(junit_xml):
    """-> {test id: passed}. id = file::classname::name."""
    out = {}
    root = ET.fromstring(junit_xml)
    for case in root.iter("testcase"):
        tid = "%s::%s::%s" % (case.get("file", ""), case.get("classname", ""), case.get("name", ""))
        bad = any(c.tag in ("failure", "error") for c in case)
        skipped = any(c.tag == "skipped" for c in case)
        if not skipped:
            out[tid] = out.get(tid, True) and not bad
    return out


def run(d):
    if not os.path.isdir(d):
        return {}                                    # the dashboard does not exist yet on this side
    if os.path.exists(os.path.join(d, "package.json")):
        subprocess.run(["bun", "install"], cwd=d, check=False)
    rep = os.path.join(d, ".ratchet-junit.xml")
    subprocess.run(["bun", "test", "--reporter=junit", "--reporter-outfile=" + rep], cwd=d, check=False)
    if not os.path.exists(rep):
        return {}                                    # no tests at all
    with open(rep, encoding="utf-8") as f:
        return results(f.read())


def broken(base, head, redefined):
    return sorted(t for t, ok in base.items()
                  if ok and not head.get(t, False) and t.split("::", 1)[0] not in redefined)


def main(argv):
    base_dir, head_dir, redefined = argv[0], argv[1], set(argv[2:])
    bad = broken(run(base_dir), run(head_dir), redefined)
    for t in bad:
        print("broken: " + t)
    print("ratchet: %s" % ("FAIL, %d passing test(s) broken" % len(bad) if bad else "ok"))
    return 1 if bad else 0


def _demo():
    x = ('<testsuites><testsuite name="a"><testcase file="test/a.test.ts" classname="A" name="one"/>'
         '<testcase file="test/a.test.ts" classname="A" name="two"><failure/></testcase>'
         '<testcase file="test/b.test.ts" classname="B" name="skip"><skipped/></testcase>'
         '</testsuite></testsuites>')
    r = results(x)
    assert r == {"test/a.test.ts::A::one": True, "test/a.test.ts::A::two": False}, r
    base = {"test/a.test.ts::A::one": True, "test/c.test.ts::C::x": True, "test/d.test.ts::D::y": False}
    head = {"test/a.test.ts::A::one": False, "test/d.test.ts::D::y": False}
    assert broken(base, head, set()) == ["test/a.test.ts::A::one", "test/c.test.ts::C::x"]
    assert broken(base, head, {"test/a.test.ts", "test/c.test.ts"}) == []
    assert broken({}, head, set()) == []
    print("bun_ratchet: all checks pass")


if __name__ == "__main__":
    if sys.argv[1:] == ["--demo"]:
        _demo()
    else:
        sys.exit(main(sys.argv[1:]))
