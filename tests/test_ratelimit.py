"""Tests for rate limiter module."""

from __future__ import annotations

import time


class TestRateLimiterInitialState:
    def test_default_initial_delay(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter()
        assert limiter.current_delay == 5.0

    def test_custom_initial_delay(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=3.0)
        assert limiter.current_delay == 3.0

    def test_zero_consecutive_429s_initially(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter()
        assert limiter.consecutive_429s == 0


class TestRateLimiter429:
    def test_429_doubles_delay(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, jitter=0.0)
        assert limiter.current_delay == 5.0

        limiter.record_429()
        assert limiter.current_delay == 10.0

    def test_429_twice_doubles_again(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, jitter=0.0)
        limiter.record_429()
        limiter.record_429()
        assert limiter.current_delay == 20.0

    def test_429_max_cap(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, max_delay=15.0, jitter=0.0)
        limiter.record_429()
        assert limiter.current_delay == 10.0
        limiter.record_429()
        assert limiter.current_delay == 15.0
        limiter.record_429()
        assert limiter.current_delay == 15.0  # Capped

    def test_429_increments_counter(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter()
        limiter.record_429()
        assert limiter.consecutive_429s == 1
        limiter.record_429()
        assert limiter.consecutive_429s == 2

    def test_429_returns_delay(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, jitter=0.0)
        delay = limiter.record_429()
        assert delay == 10.0


class TestRateLimiterSuccess:
    def test_success_decreases_delay(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=10.0, jitter=0.0)

        limiter.record_429()  # 20s
        assert limiter.current_delay == 20.0

        limiter.record_success()
        assert limiter.current_delay < 20.0

    def test_success_resets_429_counter(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter()
        limiter.record_429()
        assert limiter.consecutive_429s == 1

        limiter.record_success()
        assert limiter.consecutive_429s == 0


class TestRateLimiterRecovery:
    def test_gradual_recovery(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, max_delay=60.0, jitter=0.0)

        limiter.record_429()  # 10s
        limiter.record_429()  # 20s
        limiter.record_429()  # 40s
        assert limiter.current_delay == 40.0

        for _ in range(20):
            limiter.record_success()

        assert limiter.current_delay < 40.0
        assert limiter.current_delay >= 5.0

    def test_recovery_converges_to_initial(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, jitter=0.0)

        limiter.record_429()  # 10s
        assert limiter.current_delay == 10.0

        for _ in range(50):
            limiter.record_success()

        assert limiter.current_delay == 5.0

    def test_successive_successes_decrease_delay(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, jitter=0.0)

        limiter.record_429()  # 10s
        initial = limiter.current_delay

        for i in range(10):
            limiter.record_success()
            assert limiter.current_delay < initial


class TestRateLimiterTiming:
    def test_first_call_returns_zero(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=2.0, jitter=0.0)
        delay = limiter.get_and_apply_delay()
        assert delay == 0.0

    def test_need_delay_after_request(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=2.0, jitter=0.0)
        limiter.update_request_time()

        delay = limiter.get_and_apply_delay()
        assert 1.9 <= delay <= 2.5  # May include random_extra

    def test_no_delay_after_waiting(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=0.5, jitter=0.0)
        limiter.update_request_time()
        time.sleep(0.6)

        delay = limiter.get_and_apply_delay()
        assert delay == 0.0


class TestMessageRateLimiter:
    def test_default_delays(self):
        from llm_archive.ratelimit import MessageRateLimiter

        limiter = MessageRateLimiter()
        assert limiter.current_delay == 5.0

    def test_custom_delays(self):
        from llm_archive.ratelimit import MessageRateLimiter

        limiter = MessageRateLimiter(initial_delay=3.0, max_delay=30.0)
        assert limiter.current_delay == 3.0


class TestRateLimiterEdgeCases:
    def test_multiple_429_then_recovery(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, max_delay=60.0, jitter=0.0)

        limiter.record_429()  # 10s
        limiter.record_429()  # 20s
        assert limiter.current_delay == 20.0

        for _ in range(50):
            limiter.record_success()

        assert limiter.current_delay == 5.0

    def test_no_delay_below_initial(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=5.0, jitter=0.0)

        for _ in range(100):
            limiter.record_success()

        assert limiter.current_delay == 5.0


class TestRateLimiterConfigurability:
    def test_custom_backoff_factor(self):
        from llm_archive.ratelimit import RateLimiter

        limiter = RateLimiter(initial_delay=10.0, backoff_factor=3.0, jitter=0.0)

        limiter.record_429()
        assert limiter.current_delay == 30.0

    def test_custom_jitter(self):
        from llm_archive.ratelimit import RateLimiter

        delays = set()
        for _ in range(20):
            limiter = RateLimiter(initial_delay=10.0, jitter=0.5)
            delay = limiter.record_429()
            delays.add(round(delay, 1))

        assert len(delays) > 1, "Jitter should produce varying delays"

    def test_no_jitter(self):
        from llm_archive.ratelimit import RateLimiter

        for _ in range(10):
            limiter = RateLimiter(initial_delay=10.0, jitter=0.0)
            delay = limiter.record_429()
            assert delay == 20.0
