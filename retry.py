"""RELIABLE pillar: retry decorator for transient Alpaca API failures (network blips, rate
limits, momentary 5xx errors). Without this, one flaky HTTP call would crash the whole hourly
run and silently skip that run entirely - exactly the kind of silent failure this project is
meant to avoid. Wraps a function so it retries with exponential backoff before giving up for
real, and prints a clear warning on every retry so it's visible in the GitHub Actions log rather
than swallowed.
"""
import functools
import time


def retry(times: int = 3, base_delay: float = 2.0, exceptions: tuple = (Exception,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == times:
                        break
                    delay = base_delay * (2 ** (attempt - 1))
                    print(f"  WARNING: {func.__name__} failed (attempt {attempt}/{times}): {e} "
                          f"- retrying in {delay:.0f}s")
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
