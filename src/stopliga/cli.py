"""Command-line entrypoint."""

from __future__ import annotations

import logging
import signal
from threading import Event
from typing import Sequence

from .config import build_parser, load_config
from .errors import (
    AlreadyRunningError,
    AuthenticationError,
    ConfigError,
    DuplicateRouteError,
    InvalidFeedError,
    NetworkError,
    RemoteRequestError,
    RouteNotFoundError,
    StopLigaError,
    UnsupportedRouteShapeError,
)
from .logging_utils import configure_logging, log_event
from .service import StopLigaService
from .state import FileLock, StateStore


def _install_signal_handlers(stop_event: Event) -> None:
    def _handle_signal(signum: int, _frame: object) -> None:
        stop_event.set()
        log_event(logging.getLogger("stopliga.cli"), logging.INFO, "signal_received", signal=signum)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def _run_healthcheck(config) -> int:
    logger = logging.getLogger("stopliga.healthcheck")
    healthy, message = StateStore(config.state_file).healthcheck(config.resolved_health_max_age())
    log_event(logger, logging.INFO if healthy else logging.ERROR, "healthcheck", healthy=healthy, message=message)
    return 0 if healthy else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        config = load_config(args, validate=not args.healthcheck)
        configure_logging(config.log_level)

        if args.healthcheck:
            return _run_healthcheck(config)

        service = StopLigaService(config)
        with FileLock(config.lock_file):
            if config.run_mode == "loop":
                stop_event = Event()
                _install_signal_handlers(stop_event)
                return service.run_loop(stop_event)
            service.run_once()
            return 0
    except ConfigError as exc:
        log_event(logging.getLogger("stopliga"), logging.ERROR, "config_error", error=exc)
        return 2
    except AuthenticationError as exc:
        log_event(logging.getLogger("stopliga"), logging.ERROR, "authentication_error", error=exc)
        return 3
    except (RouteNotFoundError, DuplicateRouteError) as exc:
        log_event(logging.getLogger("stopliga"), logging.ERROR, "route_error", error=exc)
        return 4
    except UnsupportedRouteShapeError as exc:
        log_event(logging.getLogger("stopliga"), logging.ERROR, "unsupported_route_shape", error=exc)
        return 5
    except AlreadyRunningError as exc:
        log_event(logging.getLogger("stopliga"), logging.ERROR, "lock_busy", error=exc)
        return 6
    except (InvalidFeedError, NetworkError, RemoteRequestError, StopLigaError) as exc:
        log_event(logging.getLogger("stopliga"), logging.ERROR, "sync_error", error=exc)
        return 10
    except KeyboardInterrupt:
        log_event(logging.getLogger("stopliga"), logging.INFO, "interrupted")
        return 130
