"""Filesystem layout for statement files (PRD §6).

Originals land under {root}/incoming/{upload_id}/ and are moved to
{root}/{period_code}/{catalog}/ by the sort stage. The root is configurable
via the STATEMENTS_STORAGE_ROOT env var so tests never write into the repo.
"""

import os

# storage.py -> statement_ingest -> services -> app -> repo root
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

_DEFAULT_ROOT = os.path.join(_REPO_ROOT, "storage", "statements")


def get_storage_root() -> str:
    """Resolved at call time (not import) so env overrides always win."""
    return os.getenv("STATEMENTS_STORAGE_ROOT") or _DEFAULT_ROOT


def incoming_dir(upload_id: int) -> str:
    return os.path.join(get_storage_root(), "incoming", str(upload_id))


def sorted_dir(period_code: str, catalog: str) -> str:
    """Destination for sorted statement files, e.g. {root}/PUB26H1/MECH/."""
    return os.path.join(get_storage_root(), period_code, catalog)


# --- portable paths ---------------------------------------------------------
#
# Statement file paths used to be stored absolute, e.g.
#   /Users/stevengarcia/VERAX_2/verax_backend/storage/statements/PUB26H1/YT/x.xlsx
# which is meaningless on any other machine: after a deploy every writer's PDF
# download 404s while the rest of the app looks perfectly healthy. Paths are now
# stored RELATIVE to the storage root ("PUB26H1/YT/x.xlsx") so the same database
# works on a laptop, a Render disk, or anywhere else the root is mounted.


def to_storage_relative(path: str) -> str:
    """Strip the storage root off a path so what's saved is portable."""
    if not path:
        return path
    normalized = os.path.normpath(path)
    root = os.path.normpath(get_storage_root())
    if normalized.startswith(root + os.sep):
        return os.path.relpath(normalized, root)
    # A path from another machine's root — keep everything after the last
    # storage/statements/ marker so old absolute values still resolve.
    marker = os.path.join("storage", "statements") + os.sep
    idx = normalized.find(marker)
    if idx != -1:
        return normalized[idx + len(marker) :]
    return normalized


def _inside_storage(resolved: str, root: str) -> bool:
    return resolved == root or resolved.startswith(root + os.sep)


def resolve_stored_path(path: str) -> str:
    """Turn a stored path into a filesystem path under the storage root.

    Handles both forms on purpose: rows written before this change still hold
    absolute paths, and re-pointing them is a data migration that must not be a
    prerequisite for the app serving files correctly. An absolute path that
    exists outside the storage root is not served. `..` cannot walk out either.
    Returns "" when the path is not inside the root; callers treat that as missing.

    The returned string keeps the configured root (it is not passed through
    realpath). realpath is only the containment check, so a symlink such as
    /var -> /private/var does not change the path callers already compare.
    """
    if not path:
        return path
    root_raw = os.path.normpath(get_storage_root())
    root = os.path.realpath(root_raw)
    normalized = os.path.normpath(path)
    if os.path.isabs(normalized) and os.path.exists(normalized):
        logical = normalized
    elif os.path.isabs(normalized):
        relative = to_storage_relative(normalized)
        if os.path.isabs(relative):
            logical = relative
        else:
            logical = os.path.normpath(os.path.join(root_raw, relative))
    else:
        logical = os.path.normpath(os.path.join(root_raw, normalized))
    if not _inside_storage(os.path.realpath(logical), root):
        return ""
    return logical
