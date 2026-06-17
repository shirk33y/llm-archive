"""
Adaptive rate limiter for API requests.

Uses exponential backoff on 429 errors with gradual recovery based on successes.
Inspired by rust adaptive-backoff and AWS jitter algorithms.

Features:
- On 429: exponential backoff with configurable factor
- On success: gradual decrease toward initial_delay
- Full jitter (AWS recommended) to avoid thundering herd and detection
- Track consecutive successes to determine when to reduce delay
- Configurable delays and caps
- Generic enough to work with any web ingestor (httpx, etc.)
"""

from __future__ import annotations

import asyncio
import random
import time

from llm_archive.logging import get_logger

logger = get_logger("ratelimit")


class RateLimiter:
    """Adaptive rate limiter with smooth convergence.

    Algorithm (inspired by rust adaptive-backoff):
    - On failure (429): multiply delay by backoff_factor, accumulate fail_factor
    - On success: decrease delay toward initial_delay using success_factor
    - Delay converges smoothly to optimal rate

    Uses "Full Jitter" for retries (AWS recommended) to avoid:
    - Thundering herd (multiple clients retrying at same time)
    - Detection as automated (predictable timing patterns)
    """

    def __init__(
        self,
        initial_delay: float = 5.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        jitter: float = 0.3,
        min_delay_after_429: float = 1.0,
    ):
        self._initial_delay = initial_delay
        self._max_delay = max_delay
        self._backoff_factor = backoff_factor
        self._jitter = jitter
        self._min_delay_after_429 = min_delay_after_429

        self._delay = initial_delay
        self._fail_factor = 1.0
        self._success_factor = 1.0
        self._consecutive_429s = 0
        self._last_request_time = 0.0  # 0 means "no previous request"
        self._just_had_429 = False

    @property
    def current_delay(self) -> float:
        return self._delay

    @property
    def consecutive_429s(self) -> int:
        return self._consecutive_429s

    def record_429(self) -> float:
        """Call after receiving a 429 rate limit response.

        Returns the delay to wait before retrying (with jitter, min 1s).
        """
        self._consecutive_429s += 1
        self._fail_factor += 1.0
        self._just_had_429 = True

        self._delay = min(self._delay * self._backoff_factor, self._max_delay)
        wait_time = self._full_jitter(self._delay)

        # Ensure minimum wait time after 429
        if wait_time < self._min_delay_after_429:
            wait_time = self._min_delay_after_429 + random.uniform(0, self._min_delay_after_429)

        return wait_time

    def record_success(self) -> None:
        """Call after a successful request."""
        self._consecutive_429s = 0
        self._just_had_429 = False
        self._success_factor += 1.0

        decrease = self._initial_delay / self._success_factor
        new_delay = self._delay - decrease

        if new_delay < self._initial_delay:
            self._delay = self._initial_delay
            logger.debug(f"At minimum delay: {self._delay:.1f}s")
        else:
            self._delay = new_delay
            if self._success_factor % 5 < 1:
                logger.debug(f"Delay decreased to {self._delay:.1f}s")

    def _full_jitter(self, delay: float) -> float:
        """Full jitter: random value between 0 and delay * jitter factor."""
        if self._jitter <= 0:
            return delay
        return random.uniform(0, delay * self._jitter)

    def _symmetric_jitter(self, delay: float) -> float:
        """Symmetric jitter: delay ± jitter_range."""
        if self._jitter <= 0:
            return delay
        jitter_range = delay * self._jitter
        return delay + random.uniform(-jitter_range, jitter_range)

    def get_delay(self) -> float:
        """Get the delay needed before next request (no side effects)."""
        if self._last_request_time == 0:
            return 0.0  # First request, no delay

        elapsed = time.time() - self._last_request_time

        if self._just_had_429:
            return self._min_delay_after_429

        base_delay = max(self._delay - elapsed, 0.0)
        random_extra = random.uniform(0, 0.5) if base_delay > 0 else 0.0
        return base_delay + random_extra

    async def wait(self) -> float:
        """Async wait for the required delay.

        Returns the actual delay waited (may be 0 if not needed).
        """
        delay = self.get_delay()
        if delay > 0:
            logger.debug(f"Rate limit: waiting {delay:.2f}s")
            await asyncio.sleep(delay)
        return delay

    def update_request_time(self) -> None:
        """Call after making a request."""
        self._last_request_time = time.time()

    def get_and_apply_delay(self) -> float:
        """Get delay and update request time (synchronous version)."""
        delay = self.get_delay()
        self.update_request_time()
        return delay

    def update_last_request_time(self) -> None:
        """Alias for update_request_time()."""
        self.update_request_time()

    async def retry_with_backoff(self, request_func, *args, **kwargs):
        """Execute a request with automatic retry on 429."""
        max_retries = 10
        last_error = None

        for attempt in range(max_retries):
            await self.wait()
            self.update_request_time()

            try:
                response = await request_func(*args, **kwargs)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        wait_time = float(retry_after)
                    else:
                        wait_time = self.record_429()
                    await asyncio.sleep(wait_time)
                    continue

                self.record_success()
                return response

            except Exception as e:
                last_error = e
                wait_time = self.record_429()
                logger.warning(f"Error: {e}. Retrying in {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)

        raise last_error or RuntimeError(f"Request failed after {max_retries} retries")


class MessageRateLimiter(RateLimiter):
    """Rate limiter tuned for ChatGPT message fetching.

    ChatGPT has aggressive rate limits that trigger after ~2 rapid requests.
    Conservative defaults help avoid triggering limits.
    """

    def __init__(
        self,
        initial_delay: float = 5.0,
        max_delay: float = 120.0,
    ):
        super().__init__(
            initial_delay=initial_delay,
            max_delay=max_delay,
            backoff_factor=2.0,
            jitter=0.3,
            min_delay_after_429=2.0,
        )
