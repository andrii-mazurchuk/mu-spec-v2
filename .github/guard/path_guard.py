#!/usr/bin/env python3
"""Path-guard: fail a PR whose diff leaves its unit's write_set.

Runs in the TARGET repo's CI, next to muspec_sidecar.py. Answers one question
mechanically -- did this branch touch only what its unit was permitted to
touch -- so that no agent has to be trusted to answer it, and Lain never has to
audit a diff for scope.

Because write sets are disjoint across kinds (and mu-spec structurally refuses
a module claiming both a spec and a test identifier), this one check also
enforces "the implementer never modifies tests" and "nobody touches CI", with
no separate rule for either.

Scope comes from the ISSUE, never from the branch. Anything the agent can write
is not evidence about what it was allowed to write.

    python3 path_guard.py --repo owner/name --branch S-76 --base main
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import muspec_sidecar as sidecar

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


def _gh(path, token):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def unit_issue(repo, key, token):
    """The issue for this unit key, from its labels.

    Found by label rather than by issue number: a withdrawal re-emits a unit
    with a NEW number, so a number is not a durable identity for a unit. And
    among several emissions of one unit, the highest cut_seq wins -- a lower cut
    still open is stale, not work.
    """
    anchor = key[:-2] if key.endswith("-T") else key
    kind = "test" if key.endswith("-T") else "implementation"
    label = "mu-spec:%s" % anchor

    found = []
    for state in ("open", "all"):          # prefer open; fall back for a merged re-run
        issues = _gh("/repos/%s/issues?state=%s&labels=%s&per_page=100"
                     % (repo, state, urllib.request.quote(label)), token)
        if not issues:
            # A revised unit keeps its sidecar key (S-178-T) but is labelled with its new
            # anchor (mu-spec:S-224): scan every issue of this kind and match the key.
            issues, page = [], 1
            while True:
                batch = _gh("/repos/%s/issues?state=%s&labels=%s&per_page=100&page=%d"
                            % (repo, state, urllib.request.quote("kind:%s" % kind), page), token)
                issues += batch
                if len(batch) < 100:
                    break
                page += 1
        for issue in issues:
            names = [l["name"] for l in issue.get("labels", [])]
            if "kind:%s" % kind not in names:
                continue
            try:
                block = sidecar.parse(issue.get("body") or "")
            except sidecar.Malformed:
                continue                   # not a unit we can guard; not ours to fix here
            if block["key"] != key:
                continue
            found.append((block["cut_seq"], issue["number"], block))
        if found:
            break

    if not found:
        raise SystemExit(
            "path-guard: no issue found for unit %s (label %s, kind:%s).\n"
            "Refusing to pass: scope must come from the issue, and there is no "
            "issue to read it from." % (key, label, kind))

    found.sort()
    return found[-1][1], found[-1][2]


def changed_paths(base):
    """Paths this branch changed against the base, rename-aware.

    Uses the merge base rather than a two-dot diff: a two-dot diff against a
    moved base attributes other people's commits to this branch, which fails a
    correct PR and teaches everyone to ignore the guard.
    """
    merge_base = subprocess.run(["git", "merge-base", "HEAD", base],
                                capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=ACMRD",
                          merge_base, "HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True, help="the unit key, verbatim")
    ap.add_argument("--base", default="origin/main")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("path-guard: GITHUB_TOKEN unset; cannot read the unit's scope")

    # The branch name IS the unit key -- no transform on either side. If this
    # raises, the branch was not created from a unit key and the guard has
    # nothing to check it against.
    key = sidecar.branch_name(args.branch)

    number, block = unit_issue(args.repo, key, token)
    write_set = block["write_set"]
    changed = changed_paths(args.base)
    violations = sidecar.outside_write_set(changed, write_set)

    print("unit %s (issue #%d, cut %s)" % (key, number, block["cut_seq"]))
    print("  write_set (%d):" % len(write_set))
    for p in write_set:
        print("    %s" % p)
    print("  changed (%d):" % len(changed))
    for p in changed:
        print("    %s%s" % ("!! " if p in violations else "   ", p))

    if not violations:
        print("\nPASS: every changed path is inside the unit's write_set.")
        return 0

    print("\nFAIL: %d path(s) outside the write_set of %s:" % (len(violations), key))
    for p in violations:
        print("    %s" % p)
    print("""
Two different things look like this, and they are not the same:

  The agent overreached. Normal. Revert the out-of-scope changes; if the work
  genuinely needs them, it is a different unit and belongs in a different issue.

  The write_set is wrong. An INCIDENT, not a PR problem. If every changed path
  is genuinely required by this unit's contract, then mu-spec's module map is
  stale and the fix is upstream. Escalate rather than widening anything here.

Never edit this workflow, the write_set, or the issue body to make this pass.""")
    return 1


if __name__ == "__main__":
    sys.exit(main())
