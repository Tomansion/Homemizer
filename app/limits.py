"""Per-IP rate limiting and the global Nominatim throttle.

Both endpoints spend something that is not ours to spend without limit: one
burns the OpenRouteService key, the other leans on a free OSM service with a
published 1-request-per-second ceiling counted across *all* of our users. These
are the guards that keep a single visitor — or a single loop — from using up
the day for everyone.
"""
import asyncio
import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window counter per client, over several windows at once.

    Windows are (limit, seconds) pairs, e.g. 20 per minute *and* 400 per day, so
    a short burst and a slow grind both get caught.
    """

    def __init__(self, windows: list[tuple[int, int]]):
        # Widest window first: it decides how much history is worth keeping.
        self.windows = sorted(windows, key=lambda w: -w[1])
        self.span = self.windows[0][1] if self.windows else 0
        self._hits: dict[str, deque] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def check(self, key: str) -> int | None:
        """Record a hit. Returns None if allowed, else seconds to wait."""
        now = time.monotonic()
        self._sweep(now)
        hits = self._hits[key]
        while hits and now - hits[0] > self.span:
            hits.popleft()
        for limit, window in self.windows:
            # Hits inside this window, counting from the newest backwards.
            n = sum(1 for t in hits if now - t <= window)
            if n >= limit:
                oldest = next(t for t in hits if now - t <= window)
                return max(1, int(window - (now - oldest)) + 1)
        hits.append(now)
        return None

    def _sweep(self, now: float) -> None:
        """Drop clients we have not heard from in a full window, so the table
        does not grow with every IP that ever visited."""
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self.span]:
            del self._hits[key]


class Throttle:
    """Guarantees a minimum gap between the calls it guards.

    The lock is held only while waiting for the slot, not for the request
    itself, so callers start at least `min_interval` apart without the requests
    being serialised end to end.
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()


def client_ip(request) -> str:
    """The caller's address as uvicorn resolved it.

    With --proxy-headers and --forwarded-allow-ips this is the real client
    behind the reverse proxy; without them every request would look like it came
    from the proxy and the limiter would rate-limit the whole site as one user.
    """
    return request.client.host if request.client else "unknown"
