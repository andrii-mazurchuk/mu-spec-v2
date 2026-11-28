"""Test ratchet: no test that passes on the base may stop passing on the head.

main is red by design (test-first: a test unit's suite fails until its implementation lands), so
"all green" is not the bar. "Nothing that worked breaks" is, and the newly passing tests show what
the PR made work.

    python3 pytest_ratchet.py <base_dir> <head_dir> [redefined test file ...]

A test unit (branch `S-…-T`) may redefine tests in the files it writes: tightening a passing test
so it fails until its implementation lands is test-first, not a regression. The workflow passes
those files; their breaks are listed, not failed. The CTO's review judges them. In one of them it
may not:
- bind a top-level name twice: a second `def _helper` silently replaces the first for every older
  test in the module (S-318-T broke 37 that way);
- import a `mu_spec` module that does not exist yet at the top level: the whole file then fails to
  collect, so one missing module hides every other test in it (tests import inside the test);
- have a test that errors in setup for a reason other than the absent implementation (a missing
  module or name, NotImplementedError): its fixture is broken, so it fails for the wrong reason.
Every test of those files that does not pass is listed with the first line of its failure.
"""
import ast
import collections
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

# what a test fails with while its implementation is absent
ABSENT = ("ModuleNotFoundError", "ImportError", "cannot import name", "AttributeError",
          "NotImplementedError")


def results(cwd):
    """-> {test id: None if it passed, else (junit tag, first line of the reason)}."""
    out = os.path.join(tempfile.mkdtemp(), "junit.xml")
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "--continue-on-collection-errors", "--junitxml", out], check=False, cwd=cwd)
    res = {}
    for tc in ET.parse(out).iter("testcase"):
        bad = [c for c in tc if c.tag in ("failure", "error", "skipped")]
        why = ((bad[0].get("message") or bad[0].text or "").strip().splitlines() or [""])[0][:200] if bad else ""
        res["%s::%s" % (tc.get("classname"), tc.get("name"))] = (bad[0].tag, why) if bad else None
    return res


def passing(res):
    return {t for t, r in res.items() if r is None}


def in_files(t, files):
    """Is test id t (junit classname "tests.test_x" or "tests.test_x.TestClass") in one of files?"""
    c = t.split("::")[0]
    return any(c == m or c.startswith(m + ".")
               for m in (os.path.splitext(f)[0].replace("/", ".") for f in files))


def compare(base, head, redefined=()):
    """-> (broken, redefined-by-this-test-unit, newly passing)."""
    lost = sorted(base - head)
    return ([t for t in lost if not in_files(t, redefined)], [t for t in lost if in_files(t, redefined)],
            sorted(head - base))


def rebound(source):
    """Top-level function/class names defined more than once in a module's source."""
    c = collections.Counter(n.name for n in ast.parse(source).body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return sorted(k for k, v in c.items() if v > 1)


def unbuilt_imports(source, head_dir):
    """Top-level `import mu_spec.x` / `from mu_spec.x import y` whose module is not in head_dir."""
    out = []
    for n in ast.parse(source).body:
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            mods = [n.module]
        else:
            continue
        for m in mods:
            path = os.path.join(head_dir, *m.split("."))
            if m.split(".")[0] == "mu_spec" and not (os.path.isdir(path) or os.path.exists(path + ".py")):
                out.append(m)
    return out


def wrong_reason(reason):
    """A setup error (a fixture failing) not caused by the absent implementation."""
    tag, why = reason
    return tag == "error" and not any(a in why for a in ABSENT)


def per_file(redefined, head_dir, check):
    found = []
    for f in redefined:
        p = os.path.join(head_dir, f)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                hits = check(fh.read())
            if hits:
                found.append("%s: %s" % (f, ", ".join(hits)))
    return found


def main(base_dir, head_dir, redefined=()):
    dup = per_file(redefined, head_dir, rebound)
    early = per_file(redefined, head_dir, lambda src: unbuilt_imports(src, head_dir))
    head = results(head_dir)
    broke, redef, fixed = compare(passing(results(base_dir)), passing(head), redefined)
    mine = {t: r for t, r in head.items() if r and in_files(t, redefined)}
    # tests/acceptance needs the old install and a store copy (S-484): CI has neither by design, forge's
    # acceptance gate runs them locally, so their setup error here is no fixture fault
    fixture = sorted(t for t, r in mine.items() if wrong_reason(r) and not t.startswith("tests.acceptance."))
    lines = ["## Test ratchet", "", "%d newly passing, %d broken, %d redefined by this test unit"
             % (len(fixed), len(broke), len(redef)), ""]
    lines += ["- REBOUND (a second top-level definition replaces the first): %s" % d for d in dup]
    lines += ["- TOP-LEVEL IMPORT of an unbuilt module (import it inside the test): %s" % d for d in early]
    lines += ["- FIXTURE ERROR (fails in setup, not for the absent implementation): %s: %s"
              % (t, mine[t][1]) for t in fixture]
    lines += ["- BROKEN: %s" % t for t in broke] + ["- redefined: %s" % t for t in redef]
    lines += ["- test unit's %s: %s: %s" % (r[0], t, r[1]) for t, r in sorted(mine.items()) if t not in fixture]
    lines += ["- now passing: %s" % t for t in fixed]
    report = "\n".join(lines)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 1 if broke or dup or early or fixture else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--demo"]:
        assert compare({"a", "b"}, {"b", "c"}) == (["a"], [], ["c"])
        assert compare({"a"}, {"a", "b"}) == ([], [], ["b"])
        base = {"tests.test_x::t1", "tests.test_y::t2"}
        assert compare(base, set(), ["tests/test_x.py"]) == (["tests.test_y::t2"], ["tests.test_x::t1"], [])
        assert in_files("tests.test_x.TestA::t", ["tests/test_x.py"])
        assert not in_files("tests.test_xy::t", ["tests/test_x.py"])
        assert rebound("def a(): pass\ndef b(): pass\ndef a(cell): pass\n") == ["a"]
        assert rebound("def a(): pass\nclass B: pass\n") == []
        root = tempfile.mkdtemp()                      # a head where mu_spec.config exists, nothing else
        os.makedirs(os.path.join(root, "mu_spec"))
        open(os.path.join(root, "mu_spec", "config.py"), "w").close()
        src = ("import os\nimport mu_spec.config\nfrom mu_spec.nosuch.mod import x\n"
               "def test_a():\n    from mu_spec.later import y\n")
        assert unbuilt_imports(src, root) == ["mu_spec.nosuch.mod"]           # inside the test is fine
        assert wrong_reason(("error", 'failed on setup with "TypeError: build_cell() missing mission"'))
        assert not wrong_reason(("error", "failed on setup with \"ModuleNotFoundError: No module named 'mu_spec.x'\""))
        assert not wrong_reason(("failure", "AssertionError: assert 1 == 2"))
        print("ratchet: all checks pass")
        sys.exit(0)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
