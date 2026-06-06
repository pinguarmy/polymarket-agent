"""
RateLimiter — Token Bucket Algorithm
======================================

The token bucket is a rate-limiting algorithm that controls how many
operations can be performed over time while allowing short bursts.

How it works:
  1. A virtual bucket holds up to `capacity` tokens.
  2. Tokens are added at a steady rate (`refill_rate` tokens every
     `refill_interval_seconds`).
  3. When an operation is requested (`allow()`), one token is removed.
     If the bucket is empty, the request is denied (False).
  4. Unused tokens accumulate up to `capacity`, enabling bursts.

This is distinct from the **leaky bucket** algorithm (see alternatives
at the bottom of this file).  Token bucket permits bursts up to the
full capacity, while leaky bucket enforces a flat processing rate by
queuing requests.
"""

import threading
import time


class RateLimiter:
    """Token-bucket rate limiter with thread safety.

    The bucket is initialised full.  Tokens are added on every call to
    ``allow()`` based on the elapsed time (lazy / passive refill), so
    no background timer or daemon thread is needed.

    Parameters
    ----------
    capacity : int
        Maximum number of tokens the bucket can hold.  This is the
        maximum burst size.  If 0, every ``allow()`` returns False.
    refill_rate : float
        Number of tokens added each ``refill_interval_seconds``.
        May exceed ``capacity`` — the effective fill is clamped to
        the bucket's remaining headroom so capacity is never exceeded.
    refill_interval_seconds : float
        Time window (in seconds) over which ``refill_rate`` tokens
        are added.  Smaller values = smoother refill; larger values
        = more bursty refill.

    Thread safety
    -------------
    Uses ``threading.Lock`` so ``allow()`` can be safely called from
    multiple threads.  The lock only guards mutation of internal state
    (``_tokens`` and ``_last_refill_epoch``), not I/O — keep the
    critical section as thin as reasonably possible.
    """

    def __init__(
        self,
        capacity: int,
        refill_rate: float,
        refill_interval_seconds: float,
    ) -> None:
        # --- validation ---------------------------------------------------
        if capacity < 0:
            raise ValueError(f"capacity must be >= 0, got {capacity}")
        if refill_rate < 0:
            raise ValueError(f"refill_rate must be >= 0, got {refill_rate}")
        if refill_interval_seconds <= 0:
            raise ValueError(
                f"refill_interval_seconds must be > 0, "
                f"got {refill_interval_seconds}"
            )

        self._capacity = capacity
        self._refill_rate = refill_rate
        self._refill_interval = refill_interval_seconds

        # Compute the per-second refill rate once to avoid repeated division.
        self._tokens_per_second = refill_rate / refill_interval_seconds

        self._tokens = float(capacity)  # start full
        self._last_refill_epoch = time.monotonic()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allow(self) -> bool:
        """Consume one token (if available) and return True, else False.

        Before checking the token count, the bucket is lazily refilled
        based on the wall-clock time elapsed since the last call.  This
        means ``allow()`` is O(1) and requires no background scheduler.

        Thread-safe.
        """
        if self._capacity <= 0:
            return False

        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    # ------------------------------------------------------------------
    # Introspection helpers (useful for monitoring / observability)
    # ------------------------------------------------------------------

    @property
    def tokens_available(self) -> float:
        """Current number of tokens in the bucket (read-only snapshot)."""
        with self._lock:
            self._refill()
            return self._tokens

    @property
    def capacity(self) -> int:
        """Maximum burst size the bucket was configured with."""
        return self._capacity

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens accumulated since the last refill.  *Caller must
        hold the lock.*"""
        now = time.monotonic()
        elapsed = now - self._last_refill_epoch
        if elapsed > 0:
            added = elapsed * self._tokens_per_second
            self._tokens = min(self._tokens + added, float(self._capacity))
            self._last_refill_epoch = now


# ======================================================================
# Usage example
# ======================================================================

if __name__ == "__main__":
    import concurrent.futures
    import random

    def worker(limiter: RateLimiter, wid: int) -> str:
        """Each worker calls allow() once and reports the result."""
        allowed = limiter.allow()
        status = "ALLOWED" if allowed else "DENIED"
        return f"  Worker-{wid:>2}: {status}  (tokens left ≈ {limiter.tokens_available:.1f})"

    def demonstrate():
        rl = RateLimiter(capacity=10, refill_rate=5, refill_interval_seconds=1.0)

        print("=== Token Bucket Demo ===")
        print(f"Capacity = 10  |  Refill = 5 every 1 s\n")

        # --- Burst 1: drain the bucket quickly ---------------------------
        print("--- 12 concurrent requests (burst, bucket full) ---")
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(worker, rl, i) for i in range(12)]
            for f in concurrent.futures.as_completed(futures):
                print(f.result())

        # The first 10 should pass; the last 2 should be denied.
        print(f"\n  Tokens after burst: {rl.tokens_available:.1f} / {rl.capacity}")
        print("  (Expect 0.0 — all 10 were consumed)\n")

        # --- Burst 2: wait for partial refill ----------------------------
        print("--- Waiting 1.2 s then firing 5 more ---")
        time.sleep(1.2)  # ~6 tokens added (5 per sec × 1.2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(worker, rl, i) for i in range(5)]
            for f in concurrent.futures.as_completed(futures):
                print(f.result())

        print(f"\n  Tokens remaining: {rl.tokens_available:.1f} / {rl.capacity}")

        # --- Edge case: capacity=0 ----------------------------------------
        print("\n--- Capacity = 0 (always deny) ---")
        rl_zero = RateLimiter(capacity=0, refill_rate=5, refill_interval_seconds=1.0)
        for i in range(3):
            print(f"  Request {i}: {'ALLOWED' if rl_zero.allow() else 'DENIED'}")

    demonstrate()

    print("\n=== Demo complete ===")


# ======================================================================
# Alternative rate-limiting algorithms
# ======================================================================

# 1.  LEAKY BUCKET  (token bucket's strict sibling)
# ------------------------------------------------
# How it works:
#   A bucket with a small hole at the bottom.  Requests drip out at a
#   constant rate.  If the bucket overflows, excess requests are either
#   queued (shaped) or dropped (policed).  Unlike token bucket, tokens
#   are *not* stored — the bucket simply limits the *drain* rate.
#
# When preferred over token bucket:
#   - You need a *smooth, constant* outflow regardless of input burst
#     size (e.g. shaping network packets to stay below a bandwidth cap).
#   - You want to queue excess requests rather than immediately deny
#     them (token bucket either drops or passes; leaky bucket can delay).
#   - You're modelling a physical resource where processing speed is
#     capped and a backlog introduces latency rather than rejection.
#
# Trade-off:  No burst absorption.  A sudden spike is either queued
# (adding latency) or dropped.  Token bucket handles short bursts
# gracefully by design.

# 2.  SLIDING WINDOW LOG / COUNTER  (time-window approach)
# --------------------------------------------------------
# How it works:
#   Maintain a timestamp log (or a counter + previous-window weight)
#   of requests within the last N seconds.  A request is allowed if
#   the count in the sliding window is below the configured limit.
#
# When preferred over token bucket:
#   - You're enforcing a **global rate limit per identity** (e.g. "max
#     100 API calls per minute per API key") and do not care about
#     intra-minute burstiness — what matters is the strict count over
#     any rolling 60-second window.
#   - You need the limit to be *absolute and predictable*: "no more
#     than X in any Y-second interval".  Token bucket can technically
#     allow bursts up to capacity + refill-rate × interval, which
#     slightly exceeds a strict sliding-window limit over short
#     sub-intervals.
#   - You're implementing across distributed nodes without a shared
#     clock where approximated sliding windows (e.g. Redis Sorted Sets
#     or the "sliding window counter" approximation) are easier to
#     coordinate than a token bucket's continuous refill state.
#
# Trade-off:  Less forgiving of bursty workloads — a client that sends
# 100 requests in the first second then goes silent is blocked for the
# rest of the minute even though the server has spare capacity.  Token
# bucket would allow that burst (up to capacity) and then throttle.
