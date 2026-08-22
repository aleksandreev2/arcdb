"""Filesystem confinement helpers for paths loaded from mutable metadata."""

from __future__ import annotations

import os
from os import PathLike


def confined_path(
    candidate: str | PathLike[str],
    root: str | PathLike[str],
    *,
    allow_root: bool = False,
) -> str | None:
    """Return a real absolute path only when it is confined below ``root``.

    Both sides are resolved through symlinks. Cross-drive paths, sibling-prefix
    paths and the root itself (unless explicitly allowed) fail closed.
    """

    try:
        raw_candidate = os.fspath(candidate) if candidate is not None else ""
        raw_root = os.fspath(root) if root is not None else ""
    except TypeError:
        return None
    if not raw_candidate or not raw_root:
        return None
    resolved_root = os.path.realpath(os.path.abspath(raw_root))
    resolved_candidate = os.path.realpath(os.path.abspath(raw_candidate))
    try:
        common = os.path.commonpath((resolved_root, resolved_candidate))
    except ValueError:
        return None
    normalized_root = os.path.normcase(resolved_root)
    normalized_candidate = os.path.normcase(resolved_candidate)
    if os.path.normcase(common) != normalized_root:
        return None
    if not allow_root and normalized_candidate == normalized_root:
        return None
    return resolved_candidate


def confined_child(
    root: str | PathLike[str],
    child: str | PathLike[str],
) -> str | None:
    """Join an untrusted child value to ``root`` and enforce confinement."""

    if child is None:
        return None
    try:
        joined = os.path.join(os.fspath(root), os.fspath(child))
    except TypeError:
        return None
    return confined_path(joined, root)
