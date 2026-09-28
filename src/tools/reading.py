"""The line window a file read and a panel preview both walk through.

Reading a file is done in chunks so a huge file cannot be held in memory, and
every line is counted even when only a window of them is kept. The tools
(``read_file``) and the read-only web preview share this, so the two can never
disagree about what a file contains or where its lines break.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

MAX_READ_BYTES = 50 * 1024

#: Large enough to avoid per-chunk overhead, small enough to bound one read.
CHUNK_BYTES = 64 * 1024
#: Everything ``str.splitlines`` treats as a line break.
_TEXT_BREAK = re.compile("[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


class _LineWindow:
    """A streaming ``str.splitlines`` that keeps only part of its input.

    File reads need an exact line count and a bounded window of line text.
    Every line is counted, including those beyond the window. A long line
    inside the window is buffered only up to the byte and optional character
    limits, plus one character to detect overflow.
    """

    def __init__(
        self, start: int, limit: int, max_bytes: int, *, line_chars: int | None = None
    ) -> None:
        self.start = start
        self.limit = limit
        self.max_bytes = max_bytes
        self.line_chars = line_chars
        self.total = 0
        self.lines: list[str] = []
        self.bytes = 0
        self._buf: list[str] = []
        self._pending = 0
        #: An unterminated final line is still a line, even when not buffered.
        self._open = False
        #: A bare CR at a chunk boundary may pair with the next chunk's LF.
        self._cr = False

    def _wanted(self, index: int) -> bool:
        if index < self.start:
            return False
        if self.limit > 0 and index >= self.start + self.limit:
            return False
        return self.bytes <= self.max_bytes

    def _keep(self, text: str) -> None:
        """Keep at most the remaining budget plus one overflow character."""
        room = self.max_bytes - self.bytes + 1 - self._pending
        if self.line_chars is not None:
            room = min(room, self.line_chars - self._pending)
        if room > 0:
            piece = text[:room]
            self._buf.append(piece)
            self._pending += len(piece)

    def feed(self, chunk: str) -> None:
        if self._cr:
            self._cr = False
            if chunk.startswith("\n"):
                # The CR already ended this line; skip its LF partner.
                chunk = chunk[1:]
        pos = 0
        for match in _TEXT_BREAK.finditer(chunk):
            if match.start() < pos:
                # The LF of a CRLF pair was already consumed with the CR.
                continue
            index = self.total
            if self._wanted(index):
                self._keep(chunk[pos : match.start()])
                line = "".join(self._buf)
                self.lines.append(line)
                self.bytes += len(line.encode("utf-8", errors="replace")) + 1
            self._buf.clear()
            self._pending = 0
            self.total = index + 1
            self._open = False
            pos = match.end()
            if match.group() == "\r":
                if pos < len(chunk):
                    if chunk[pos] == "\n":
                        pos += 1
                else:
                    self._cr = True
        if pos < len(chunk):
            self._open = True
            if self._wanted(self.total):
                self._keep(chunk[pos:])

    def finish(self) -> None:
        """Close the last line when the text did not end on a line break."""
        if not self._open:
            return
        index = self.total
        if self._wanted(index):
            self.lines.append("".join(self._buf))
        self._buf.clear()
        self._pending = 0
        self.total = index + 1
        self._open = False


def read_window(
    target: Path,
    start: int,
    limit: int,
    max_bytes: int = MAX_READ_BYTES,
    *,
    line_chars: int | None = None,
) -> _LineWindow:
    """Read one file window in chunks, decoding across chunk boundaries."""
    window = _LineWindow(start, limit, max_bytes, line_chars=line_chars)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                window.feed(text)
    tail = decoder.decode(b"", final=True)
    if tail:
        window.feed(tail)
    window.finish()
    return window
