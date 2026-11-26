"""Domain-specific exceptions for StopLiga."""


class StopLigaError(Exception):
    """Base exception for all sync errors."""


class ConfigError(StopLigaError):
    """Raised when runtime configuration is invalid."""


class AuthenticationError(StopLigaError):
    """Raised when UniFi authentication fails."""


class DiscoveryError(StopLigaError):
    """Raised when the UniFi API surface cannot be discovered safely."""


class NetworkError(StopLigaError):
    """Raised when network I/O fails after retries."""


class RemoteRequestError(StopLigaError):
    """Raised when a remote endpoint returns an invalid or unexpected response."""


class HTTPStatusError(RemoteRequestError):
    """A non-success HTTP response with a known status code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class InvalidFeedError(StopLigaError):
    """Raised when the GitHub feed is malformed or unsafe to apply."""


class StaleFeedError(InvalidFeedError):
    """Raised when the feed's timestamp exceeds the configured age limit."""


class RouteNotFoundError(StopLigaError):
    """Raised when the target route does not exist."""


class DuplicateRouteError(StopLigaError):
    """Raised when more than one route matches the requested name."""


class UnsupportedRouteShapeError(StopLigaError):
    """Raised when a route payload cannot be updated safely."""


class AlreadyRunningError(StopLigaError):
    """Raised when another process already owns the local lock file."""


class StateError(StopLigaError):
    """Raised when state persistence or lock management fails."""


class NotificationDeliveryError(StopLigaError):
    """Raised when one or more notification providers fail."""

    def __init__(self, failures: dict[str, str]):
        self.failures = failures
        joined = " | ".join(f"{provider}: {error}" for provider, error in failures.items())
        super().__init__(f"Notification delivery failed: {joined}")


class ReconciliationRequiredError(StopLigaError):
    """Raised when runtime state indicates a previous partial failure still needs attention."""


class PartialUpdateError(StopLigaError):
    """Raised when a multi-step remote update partially succeeds before failing."""

    def __init__(
        self,
        failed_stage: str,
        completed_stages: tuple[str, ...],
        message: str,
        *,
        rollback_attempted: bool = False,
        rollback_completed: bool = False,
        rollback_error: str | None = None,
    ):
        super().__init__(message)
        self.failed_stage = failed_stage
        self.completed_stages = completed_stages
        self.rollback_attempted = rollback_attempted
        self.rollback_completed = rollback_completed
        self.rollback_error = rollback_error

    @property
    def stage(self) -> str:
        """Backward-compatible alias for the failed stage name."""

        return self.failed_stage
