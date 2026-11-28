"""The ordering key for every enumeration the unit returns or iterates over.

Stored order is used as stored; a list built from a set, an in-memory mapping or a directory listing
is put through by_id or names_sorted before it affects an answer, so no answer follows a directory
listing, a hash or a set.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


def id_key(identifier: str) -> tuple[str, int, str]:
    """'S-387 (was S-41)' -> ('S', 41, 'S-387 (was S-41)'); no numeric tail -> (identifier, -1, identifier)."""
    head, sep, _ = identifier.partition("-")
    tail = identifier.rpartition("-")[2]
    digits = ""
    for ch in tail:
        if not ("0" <= ch <= "9"):
            break
        digits += ch
    if not sep or not digits:
        return (identifier, -1, identifier)
    return (head, int(digits, 10), identifier)


def by_id(items: Iterable[T], ident: Callable[[T], str]) -> list[T]:
    return sorted(items, key=lambda item: id_key(ident(item)))


def names_sorted(names: Iterable[str]) -> list[str]:
    return sorted(names)
