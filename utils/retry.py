import functools
import logging
import time
import typing as tp

F = tp.TypeVar("F", bound=tp.Callable[..., tp.Any])


def retry(
    max_attempts: int = 3,
    backoff: float = 1.5,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> tp.Callable[[F], F]:
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: tp.Any, **kwargs: tp.Any) -> tp.Any:
            logger = logging.getLogger(func.__module__)
            delay = 1.0
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    logger.warning(
                        "[%s] attempt %d/%d failed: %s",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    if attempt < max_attempts:
                        time.sleep(delay)
                        delay *= backoff
            raise tp.cast(Exception, last_exc)

        return tp.cast(F, wrapper)

    return decorator
