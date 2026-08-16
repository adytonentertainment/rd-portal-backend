"""Ingested uploads must not leave duplicate copies behind.

The sorter COPIES incoming files into {root}/{period}/{catalog}/, so every
ingested file existed twice. Two real drops had left 2.0 GB of exact duplicates
on a disk that was 100% full.
"""

import os

from app.services.statement_ingest.storage import incoming_dir, sorted_dir
from app.services.statement_ingest.worker import _discard_incoming


def _write(path, body=b"data"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)


def test_incoming_copy_is_dropped_once_the_file_is_sorted(tmp_path, monkeypatch):
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path))
    name = "Ben_PUB26H1_C00001_Someone (YouTube Publishing).xlsx"
    _write(os.path.join(incoming_dir(7), name))
    _write(os.path.join(sorted_dir("PUB26H1", "YT"), name))

    _discard_incoming(7)

    assert not os.path.exists(incoming_dir(7)), "empty incoming dir should be removed"
    assert os.path.exists(os.path.join(sorted_dir("PUB26H1", "YT"), name)), "sorted copy is the keeper"


def test_a_file_with_no_sorted_copy_is_kept(tmp_path, monkeypatch):
    """Never delete the only copy of a file — an unsorted leftover stays put."""
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path))
    sorted_name = "Ben_PUB26H1_C00001_Someone (YouTube Publishing).xlsx"
    orphan = "Ben_PUB26H1_C00002_Unsorted (YouTube Publishing).xlsx"
    _write(os.path.join(incoming_dir(8), sorted_name))
    _write(os.path.join(incoming_dir(8), orphan))
    _write(os.path.join(sorted_dir("PUB26H1", "YT"), sorted_name))

    _discard_incoming(8)

    left = os.listdir(incoming_dir(8))
    assert left == [orphan], f"kept the wrong files: {left}"


def test_missing_incoming_dir_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path))
    _discard_incoming(999)  # must not raise
