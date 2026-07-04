from app.models import RetryStrategy


def compute_backoff(
    strategy: str, base_delay_s: float, max_delay_s: float, attempt: int
) -> float:
    """Delay before retry number `attempt` (1-based: first retry = attempt 1).

    fixed:        base, base, base, ...
    linear:       base, 2*base, 3*base, ...
    exponential:  base, 2*base, 4*base, 8*base, ...
    All capped at max_delay_s.
    """
    attempt = max(1, attempt)
    if strategy == RetryStrategy.LINEAR:
        delay = base_delay_s * attempt
    elif strategy == RetryStrategy.EXPONENTIAL:
        delay = base_delay_s * (2 ** (attempt - 1))
    else:  # FIXED (and safe default for unknown values)
        delay = base_delay_s
    return min(delay, max_delay_s)
