"""Rewrite statement file paths to be relative to the storage root.

Every statement stored an absolute path from the machine that ingested it:

    /Users/stevengarcia/VERAX_2/verax_backend/storage/statements/PUB26H1/YT/x.xlsx

That path does not exist anywhere else, so on a deployed instance every PDF and
XLSX download 404s while the rest of the app looks completely healthy — the
worst kind of failure, because nothing errors. Stored relative

    PUB26H1/YT/x.xlsx

the same row resolves correctly under whatever storage root the host mounts.

The transform keeps everything after the last `storage/statements/` marker, so
it works regardless of whose home directory the original path came from, and is
idempotent: a path that is already relative has no marker and is left alone.

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
"""

import posixpath

import sqlalchemy as sa
from alembic import op

revision = "x9y0z1a2b3c4"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None

MARKER = "storage/statements/"


def _relative(path):
    if not path:
        return path
    # Normalise Windows separators before looking for the marker.
    p = path.replace("\\", "/")
    idx = p.rfind(MARKER)
    if idx == -1:
        return path  # already relative, or somewhere unexpected — leave it
    return posixpath.normpath(p[idx + len(MARKER) :])


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, pdf_path, xlsx_path FROM statement "
                "WHERE pdf_path IS NOT NULL OR xlsx_path IS NOT NULL")
    ).fetchall()

    changed = 0
    for row_id, pdf_path, xlsx_path in rows:
        new_pdf, new_xlsx = _relative(pdf_path), _relative(xlsx_path)
        if new_pdf == pdf_path and new_xlsx == xlsx_path:
            continue
        conn.execute(
            sa.text("UPDATE statement SET pdf_path = :p, xlsx_path = :x WHERE id = :i"),
            {"p": new_pdf, "x": new_xlsx, "i": row_id},
        )
        changed += 1
    print(f"[migration] rewrote {changed} statement path pair(s) to be storage-relative")


def downgrade() -> None:
    # Absolute paths were machine-specific; there is no correct machine to
    # restore them to. resolve_stored_path() reads both forms, so leaving them
    # relative is safe rather than reconstructing a wrong absolute path.
    pass
