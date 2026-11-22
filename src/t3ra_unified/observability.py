import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, Token
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, TypeVar, cast

from t3ra_unified.config import get_settings

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
_shipment_id_ctx: ContextVar[str] = ContextVar("shipment_id", default="-")
_event_key_ctx: ContextVar[str] = ContextVar("event_key", default="-")
_logging_configured = False


def get_request_id() -> str:
    return _request_id_ctx.get()


def set_request_id(request_id: str | None) -> Token:
    return _request_id_ctx.set(str(request_id or "-"))


def reset_request_id(token: Token) -> None:
    _request_id_ctx.reset(token)


def get_shipment_id() -> str:
    return _shipment_id_ctx.get()


def set_shipment_id(shipment_id: str | None) -> Token:
    return _shipment_id_ctx.set(str(shipment_id or "-"))


def reset_shipment_id(token: Token) -> None:
    _shipment_id_ctx.reset(token)


def get_event_key() -> str:
    return _event_key_ctx.get()


def set_event_key(event_key: str | None) -> Token:
    return _event_key_ctx.set(str(event_key or "-"))


def reset_event_key(token: Token) -> None:
    _event_key_ctx.reset(token)


@contextmanager
def bind_shipment_id(shipment_id: str | None):
    token = set_shipment_id(shipment_id)
    try:
        yield
    finally:
        reset_shipment_id(token)


@contextmanager
def bind_request_id(request_id: str | None):
    token = set_request_id(request_id)
    try:
        yield
    finally:
        reset_request_id(token)


@contextmanager
def bind_event_key(event_key: str | None):
    token = set_event_key(event_key)
    try:
        yield
    finally:
        reset_event_key(token)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        if not hasattr(record, "shipment_id"):
            record.shipment_id = get_shipment_id()
        if not hasattr(record, "event_key"):
            record.event_key = get_event_key()
        return True


def configure_logging(
    service_name: str = "covered-loads-webhook",
    level_override: str | None = None,
) -> None:
    global _logging_configured
    if _logging_configured:
        return

    level_name = (level_override or get_settings().log_level).upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        logging.Formatter(
            fmt=(
                "%(asctime)s %(levelname)s %(name)s "
                "service=%(service_name)s request_id=%(request_id)s "
                "shipment_id=%(shipment_id)s event_key=%(event_key)s %(message)s"
            )
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    root.addFilter(ContextFilter())

    logging.LoggerAdapter(logging.getLogger(__name__), {"service_name": service_name})
    _ensure_service_name(service_name)
    _logging_configured = True


def _ensure_service_name(service_name: str) -> None:
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        if not hasattr(record, "service_name"):
            record.service_name = service_name
        return record

    logging.setLogRecordFactory(record_factory)


def configure_logfire(service_name: str = "covered-loads-webhook") -> None:
    logger = logging.getLogger(__name__)
    token_setting = get_settings().logfire_token
    if not token_setting:
        logger.info("LOGFIRE_TOKEN not set; logs stay local/stdout only")
        return
    token = token_setting.get_secret_value()

    try:
        import logfire
    except Exception:
        logger.exception("logfire package missing; install `logfire` to export logs")
        return

    try:
        logfire.configure(token=token, service_name=service_name)
        if hasattr(logfire, "instrument_logging"):
            logfire.instrument_logging()
        # Ensure stdlib `logging` records are exported to Logfire.
        root_logger = logging.getLogger()
        if not any(type(h).__name__ == "LogfireLoggingHandler" for h in root_logger.handlers):
            lf_handler = logfire.LogfireLoggingHandler()
            lf_handler.setLevel(root_logger.level)
            root_logger.addHandler(lf_handler)
        logger.info("Logfire configured and enabled")
    except Exception:
        logger.exception("Failed to initialize Logfire")


def instrument_fastapi(app: Any) -> None:
    """Attach Logfire request tracing. Optional -- the app runs fine without it."""
    logger = logging.getLogger(__name__)
    try:
        import logfire
    except ImportError:
        logger.info("logfire not installed; FastAPI tracing disabled")
        return

    try:
        logfire.instrument_fastapi(app)
        logger.info("Logfire FastAPI instrumentation enabled")
    except ImportError:
        # The `logfire[fastapi]` extra pulls in the OpenTelemetry ASGI
        # instrumentation this needs. Not fatal -- logs still export.
        logger.warning(
            "Logfire FastAPI tracing unavailable; install the logfire[fastapi] extra"
        )
    except Exception:
        logger.exception("Failed to instrument FastAPI with Logfire")


@contextmanager
def logfire_span(name: str, **attrs: Any) -> Iterator[None]:
    """Open a Logfire span if Logfire is available, otherwise do nothing.

    Only *starting* the span is guarded. Exceptions raised by the wrapped body
    propagate untouched -- an earlier version caught them and yielded a second
    time, which turned every real error into an unrelated
    ``RuntimeError: generator didn't stop after throw()``.
    """
    with ExitStack() as stack:
        try:
            import logfire

            stack.enter_context(logfire.span(name, **attrs))
        except Exception:  # noqa: BLE001 - tracing must never break the caller
            logging.getLogger(__name__).debug("Logfire span unavailable", exc_info=True)
        yield


#: Preserves the decorated function's own signature for type checkers.
F = TypeVar("F", bound=Callable[..., Any])


def traced_function(name: str | None = None) -> Callable[[F], F]:
    """
    Lightweight function tracer:
    - emits debug start/success logs
    - emits exception logs with stacktrace
    - wraps execution inside Logfire span when available
    """

    def decorator(fn: F) -> F:
        fn_name = name or f"{fn.__module__}.{fn.__qualname__}"
        logger = logging.getLogger(fn.__module__)

        if iscoroutinefunction(fn):
            @wraps(fn)
            async def async_wrapper(*args, **kwargs):
                logger.debug("Function start", extra={"function_name": fn_name})
                with logfire_span(f"fn:{fn_name}", function_name=fn_name):
                    try:
                        result = await fn(*args, **kwargs)
                        logger.debug("Function success", extra={"function_name": fn_name})
                        return result
                    except Exception:
                        logger.exception("Function failed", extra={"function_name": fn_name})
                        raise

            return cast(F, async_wrapper)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            logger.debug("Function start", extra={"function_name": fn_name})
            with logfire_span(f"fn:{fn_name}", function_name=fn_name):
                try:
                    result = fn(*args, **kwargs)
                    logger.debug("Function success", extra={"function_name": fn_name})
                    return result
                except Exception:
                    logger.exception("Function failed", extra={"function_name": fn_name})
                    raise

        return cast(F, wrapper)

    return decorator
