"""Publishing to writer portals is blocked while any ingest is in flight.

Statement numbers exist only after parse; a mid-ingest send would push partial
figures to real writers and then silently change under them at the next parse
commit. The gate lives in the publish service, so every route shares it.
"""

import pytest

from app.models.statements import StatementUpload, UploadStatus
from app.services.distribution.publish import GateNotReady, assert_no_ingest_in_flight


def _upload(session, status):
    u = StatementUpload(file_count=1, status=status)
    session.add(u)
    session.commit()
    return u


@pytest.mark.parametrize("status", [
    UploadStatus.UPLOADED, UploadStatus.SORTING, UploadStatus.PARSING,
])
def test_any_in_flight_upload_blocks_sending(session, status):
    _upload(session, status)
    with pytest.raises(GateNotReady) as exc:
        assert_no_ingest_in_flight(session)
    assert "ingest" in exc.value.gate["reasons"][0].lower()


def test_terminal_uploads_do_not_block(session):
    _upload(session, UploadStatus.DONE)
    _upload(session, UploadStatus.FAILED)
    assert_no_ingest_in_flight(session)  # must not raise


def test_distribute_batch_refuses_mid_ingest(session):
    """The gate fires before any batch inspection — a bogus batch id is enough
    to prove the ingest check comes first."""
    from app.services.distribution.publish import distribute_batch

    _upload(session, UploadStatus.PARSING)
    with pytest.raises(GateNotReady) as exc:
        distribute_batch(session, batch_id=999999)
    assert "ingest" in exc.value.gate["reasons"][0].lower()
