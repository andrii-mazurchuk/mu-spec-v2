"""The reproducible-build check: two staged builds of the page, judged against each other and the committed page.

Development and test only; never imported by mu_spec. It never consults version control and reads
under dashboard/ only the files the manifest lists.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pagebuild.manifest import ManifestError, pinned_version, read_manifest, stage

BUILDER = "bun"
BUILD_COMMAND = ("bun", "run", "build.ts")   # run with cwd = the scratch folder, the same for both builds
OUT_DIR = "out"                               # S-13 writes its one page, OUT_PAGE, here
OUT_PAGE = "out/dashboard.html"               # as declared by dashboard/build.ts (S-13)
MIN_GAP_S = 2.0                  # the second build starts at least this long after the first build has FINISHED
BUILD_TIMEOUT_S = 120.0          # the build allowance, per build

COMMITTED_PAGE = "mu_spec/dashboard.html"

VERDICT_KINDS = ("pass", "manifest_fault", "builder_missing", "builder_version_mismatch",
                 "build_failed", "not_one_page", "not_reproducible", "page_differs")


@dataclass(frozen=True)
class BuildVerdict:
    kind: str        # one of VERDICT_KINDS
    message: str     # one line, the template filled in


class _BuildFailed(Exception):
    pass


def _manifest_fault(error: ManifestError) -> BuildVerdict:
    entry = "-" if error.entry is None else error.entry
    return BuildVerdict("manifest_fault", f"manifest fault: {error.reason} {entry}")


def _run_build(builder: str, scratch: Path) -> None:
    """Run BUILD_COMMAND in scratch; raise _BuildFailed with the message detail on failure."""
    try:
        done = subprocess.run(
            [builder, *BUILD_COMMAND[1:]], cwd=scratch, capture_output=True, timeout=BUILD_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired:
        raise _BuildFailed("timed out") from None
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise _BuildFailed(lines[0].strip() if lines else "")


def _out_files(scratch: Path) -> list[str]:
    out = scratch / OUT_DIR
    if not out.is_dir():
        return []
    return sorted(p.relative_to(scratch).as_posix() for p in out.rglob("*") if p.is_file())


def check_build(repo_root: Path) -> BuildVerdict:
    """Build the page twice from its manifest in fresh scratch folders and judge both builds."""
    source = repo_root / "dashboard"
    try:
        entries = read_manifest(source)
    except ManifestError as error:
        return _manifest_fault(error)

    with tempfile.TemporaryDirectory(prefix="pagebuild-a-", ignore_cleanup_errors=True) as first_dir, \
            tempfile.TemporaryDirectory(prefix="pagebuild-b-", ignore_cleanup_errors=True) as second_dir:
        scratches = (Path(first_dir), Path(second_dir))
        try:
            for scratch in scratches:
                stage(source, entries, scratch)
        except ManifestError as error:
            return _manifest_fault(error)

        missing = BuildVerdict("builder_missing", f"page builder not installed: {BUILDER}")
        builder = shutil.which(BUILDER)
        if builder is None:
            return missing
        try:
            found = subprocess.run([builder, "--version"], capture_output=True, check=False).stdout
        except OSError:
            return missing
        found_version = found.decode("utf-8", errors="replace").strip()
        pinned = pinned_version(scratches[0])
        if found_version != pinned:
            return BuildVerdict(
                "builder_version_mismatch", f"page builder version {found_version} differs from pinned {pinned}",
            )

        try:
            _run_build(builder, scratches[0])
            t_end = time.monotonic()
            while (left := t_end + MIN_GAP_S - time.monotonic()) > 0:
                time.sleep(left)
            _run_build(builder, scratches[1])
        except _BuildFailed as failed:
            return BuildVerdict("build_failed", f"build failed: {failed}")
        except OSError:
            return missing

        pages = []
        for scratch in scratches:
            files = _out_files(scratch)
            if files != [OUT_PAGE]:
                return BuildVerdict(
                    "not_one_page", f"build produced {len(files)} files under {OUT_DIR}/, expected exactly one page",
                )
            pages.append((scratch / OUT_PAGE).read_bytes())

    if pages[0] != pages[1]:
        return BuildVerdict("not_reproducible", "build not reproducible: two fresh builds differ")
    committed = repo_root / COMMITTED_PAGE
    if not committed.is_file() or committed.read_bytes() != pages[0]:
        return BuildVerdict("page_differs", f"committed page {COMMITTED_PAGE} differs from a fresh build")
    return BuildVerdict("pass", "committed page equals a fresh build")
