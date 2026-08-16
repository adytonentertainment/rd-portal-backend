"""Stream a multipart upload straight to disk.

`await request.form()` materialises the WHOLE upload before the handler body
runs: every part becomes a SpooledTemporaryFile held open at once. A real drop
is ~5,200 files / ~2 GB, which meant roughly a gigabyte resident plus hundreds
of open descriptors, and the copy loop afterwards blocked the event loop for
minutes — the API was frozen for every other user, and an OOM kill on a small
VPS was a coin flip.

Here the body is parsed incrementally and each part is written to its final
destination as its bytes arrive, so peak memory is one chunk regardless of how
many files are sent. The `async for` over the request stream yields between
chunks, so other requests keep being served.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, Iterable, List, Optional

from multipart.multipart import MultipartParser, parse_options_header


class UploadStreamError(Exception):
    """Malformed multipart body, or a limit was exceeded."""


def _safe_name(raw: str) -> str:
    """Filename only — never a client-supplied path."""
    return os.path.basename((raw or "").replace("\\", "/")).strip()


class _PartWriter:
    """Writes one multipart part to a file, chosen by its filename."""

    def __init__(self, dest_dir: str, field_name: str, max_files: int):
        self.dest_dir = dest_dir
        self.field_name = field_name
        self.max_files = max_files
        self.written: List[str] = []
        self.skipped_field: List[str] = []
        self.bytes_written = 0

        self._header_field = b""
        self._header_value = b""
        self._headers: Dict[bytes, bytes] = {}
        self._handle = None
        self._current: Optional[str] = None

    # --- parser callbacks (sync, called as bytes arrive) --------------------

    def on_part_begin(self) -> None:
        self._headers = {}
        self._header_field = b""
        self._header_value = b""
        self._handle = None
        self._current = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._header_field.lower()] = self._header_value
        self._header_field = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        disposition = self._headers.get(b"content-disposition", b"")
        _, params = parse_options_header(disposition)
        name = (params.get(b"name") or b"").decode("utf-8", "replace")
        filename = params.get(b"filename")
        if filename is None:
            return  # a plain form field, not a file
        filename = _safe_name(filename.decode("utf-8", "replace"))
        if not filename:
            return
        if name != self.field_name:
            self.skipped_field.append(filename)
            return
        if len(self.written) >= self.max_files:
            raise UploadStreamError(
                f"Too many files (limit {self.max_files} per request)"
            )
        self._current = filename
        # Written directly to its destination — nothing is buffered in memory.
        self._handle = open(os.path.join(self.dest_dir, filename), "wb")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._handle is not None:
            chunk = data[start:end]
            self._handle.write(chunk)
            self.bytes_written += len(chunk)

    def on_part_end(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            if self._current:
                self.written.append(self._current)
        self._current = None

    def on_end(self) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


async def stream_upload_to_dir(
    request,
    dest_dir: str,
    *,
    field_name: str = "files",
    max_files: int = 20000,
    chunk_source: Optional[Callable[[], Iterable[bytes]]] = None,
) -> dict:
    """Consume the request body, writing every file part into `dest_dir`.

    Returns {"written": [names], "bytes": n}. Raises UploadStreamError on a
    malformed body or when the file limit is exceeded.
    """
    content_type = request.headers.get("content-type", "")
    _ctype, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if not boundary:
        raise UploadStreamError("Expected a multipart/form-data body")

    os.makedirs(dest_dir, exist_ok=True)
    writer = _PartWriter(dest_dir, field_name, max_files)
    callbacks = {
        "on_part_begin": writer.on_part_begin,
        "on_header_field": writer.on_header_field,
        "on_header_value": writer.on_header_value,
        "on_header_end": writer.on_header_end,
        "on_headers_finished": writer.on_headers_finished,
        "on_part_data": writer.on_part_data,
        "on_part_end": writer.on_part_end,
        "on_end": writer.on_end,
    }
    parser = MultipartParser(boundary, callbacks)

    try:
        async for chunk in request.stream():
            if chunk:
                parser.write(chunk)
        parser.finalize()
    except UploadStreamError:
        writer.close()
        raise
    except Exception as exc:  # malformed body, client disconnect mid-stream
        writer.close()
        raise UploadStreamError(str(exc)) from exc
    finally:
        writer.close()

    return {
        "written": writer.written,
        "bytes": writer.bytes_written,
        "ignored_fields": writer.skipped_field,
    }
