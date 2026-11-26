"""The page source's committed build manifest: reading it and staging exactly its files.

Development and test only; never imported by mu_spec. Neither function writes to source_root.
"""

from __future__ import annotations

import shutil
from pathlib import Path

MANIFEST_NAME = "manifest.txt"
VERSION_NAME = "bun-version"

REASONS = ("manifest_absent", "not_utf8", "carriage_return", "no_final_newline", "blank_line",
           "unsafe_path", "duplicate", "unordered", "self_unlisted",
           "version_unlisted", "listed_file_missing")


class ManifestError(Exception):
    """A fault in the manifest or in what it lists; entry is None for whole-file faults."""

    def __init__(self, entry: str | None, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown manifest fault reason: {reason!r}")
        self.entry = entry
        self.reason = reason
        super().__init__(reason if entry is None else f"{reason}: {entry!r}")


def _is_safe(entry: str) -> bool:
    if entry.startswith("/") or "\\" in entry:
        return False
    segments = entry.split("/")
    first = segments[0]
    if len(first) >= 2 and first[1] == ":" and first[0].isascii() and first[0].isalpha():
        return False
    return all(segment not in ("", ".", "..") for segment in segments)


def _is_file_under(root: Path, path: Path) -> bool:
    if not path.is_file():
        return False
    return path.resolve().is_relative_to(root)


def read_manifest(source_root: Path) -> tuple[str, ...]:
    """Parse source_root/manifest.txt, read as bytes. Returns the entries in file order.
    Raises ManifestError with the first fault found, in this order: manifest_absent, not_utf8,
    then carriage_return (entry None) when the bytes hold any b'\\r', then the line checks
    top to bottom; 'self_unlisted' and 'version_unlisted' are checked after every line."""
    path = source_root / MANIFEST_NAME
    if not path.is_file():
        raise ManifestError(None, "manifest_absent")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError(None, "not_utf8") from None
    if b"\r" in data:
        raise ManifestError(None, "carriage_return")
    if not text.endswith("\n"):
        raise ManifestError(None, "no_final_newline")

    entries: list[str] = []
    seen: set[str] = set()
    for line in text[:-1].split("\n"):
        if line == "":
            raise ManifestError(None, "blank_line")
        if not _is_safe(line):
            raise ManifestError(line, "unsafe_path")
        if line in seen:
            raise ManifestError(line, "duplicate")
        if entries and line.encode("utf-8") <= entries[-1].encode("utf-8"):
            raise ManifestError(line, "unordered")
        entries.append(line)
        seen.add(line)

    if MANIFEST_NAME not in seen:
        raise ManifestError(MANIFEST_NAME, "self_unlisted")
    if VERSION_NAME not in seen:
        raise ManifestError(VERSION_NAME, "version_unlisted")
    return tuple(entries)


def stage(source_root: Path, entries: tuple[str, ...], scratch: Path) -> None:
    """scratch must exist and be empty (else ValueError). Copies the bytes of each
    listed file to the same relative place under scratch, creating parent folders,
    in entry order. Copies content only: no timestamps or permissions are carried.
    A listed entry that is not a regular file under source_root raises
    ManifestError(entry, 'listed_file_missing') naming the first such entry; nothing
    not listed is ever copied."""
    if not scratch.is_dir() or any(scratch.iterdir()):
        raise ValueError(f"scratch must be an existing empty folder: {scratch}")
    root = source_root.resolve()
    for entry in entries:
        if not _is_safe(entry) or not _is_file_under(root, root / entry):
            raise ManifestError(entry, "listed_file_missing")
    for entry in entries:
        target = scratch / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / entry, target)


def pinned_version(scratch: Path) -> str:
    """The text of scratch/bun-version with the one final newline removed."""
    return (scratch / VERSION_NAME).read_bytes().decode("utf-8").removesuffix("\n")
