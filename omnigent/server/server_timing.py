"""Per-request phase timing, published as a ``Server-Timing`` response header.

This module is FORK-OWNED. A handler starts a :class:`RequestTiming`, records
named phases with :meth:`RequestTiming.mark` or :meth:`RequestTiming.phase`,
and the timing is written to the injected FastAPI ``Response`` as
``Server-Timing: auth;dur=3.1, reads;dur=12.0, ..., total;dur=20.4``.
FastAPI copies the injected response's headers onto the real response after it
serializes the body, so :func:`encode_phase` (wrapped around serialization in
:mod:`omnigent.server.fast_json`) can still add ``encode`` and the final total.

It is always on and costs a few ``time.perf_counter`` calls per request. A
request whose total passes :data:`SLOW_REQUEST_S` logs one WARNING line with
its phases.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from starlette.responses import Response

_logger = logging.getLogger(__name__)

SLOW_REQUEST_S = 1.0
HEADER = "Server-Timing"

_current: ContextVar[RequestTiming | None] = ContextVar("omnigent_request_timing", default=None)


class RequestTiming:
    """Named phase durations for one request.

    Phases may overlap (concurrent reads each report their own duration);
    ``total`` is always the wall time since :func:`start`.
    """

    def __init__(self, name: str, response: Response | None, subject: str | None) -> None:
        self.name = name
        self.subject = subject
        self._response = response
        self._started = time.perf_counter()
        self._last = self._started
        self._phases: dict[str, float] = {}
        self._warned = False

    def add(self, phase: str, seconds: float) -> None:
        """Add *seconds* to *phase* (repeated phases accumulate)."""
        self._phases[phase] = self._phases.get(phase, 0.0) + seconds

    def mark(self, phase: str) -> None:
        """Record the time since the previous mark (or the start) as *phase*."""
        now = time.perf_counter()
        self.add(phase, now - self._last)
        self._last = now

    @contextmanager
    def phase(self, phase: str) -> Iterator[None]:
        """Time the enclosed block as *phase*."""
        started = time.perf_counter()
        try:
            yield
        finally:
            now = time.perf_counter()
            self.add(phase, now - started)
            self._last = now

    def phases(self) -> dict[str, float]:
        """Phase durations in seconds, plus ``total``."""
        return {**self._phases, "total": time.perf_counter() - self._started}

    def header_value(self) -> str:
        """The ``Server-Timing`` value, durations in milliseconds."""
        return ", ".join(f"{name};dur={sec * 1000:.1f}" for name, sec in self.phases().items())

    def publish(self) -> None:
        """Write the header now and log once if the request is already slow."""
        phases = self.phases()
        value = ", ".join(f"{name};dur={sec * 1000:.1f}" for name, sec in phases.items())
        if self._response is not None:
            self._response.headers[HEADER] = value
        if not self._warned and phases["total"] > SLOW_REQUEST_S:
            self._warned = True
            _logger.warning(
                "slow %s subject=%s total=%.0fms phases: %s",
                self.name,
                self.subject,
                phases["total"] * 1000,
                value,
                extra={"session_id": self.subject},
            )


def start(name: str, response: Response | None, subject: str | None = None) -> RequestTiming:
    """Start timing the current request and make it :func:`current`."""
    timing = RequestTiming(name, response, subject)
    _current.set(timing)
    return timing


def current() -> RequestTiming | None:
    """The timing started by the running request, if any."""
    return _current.get()


@contextmanager
def encode_phase() -> Iterator[None]:
    """Time response serialization as ``encode`` and publish the final header."""
    timing = _current.get()
    if timing is None:
        yield
        return
    with timing.phase("encode"):
        yield
    timing.publish()
    _current.set(None)
