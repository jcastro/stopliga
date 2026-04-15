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


class InvalidFeedError(StopLigaError):
    """Raised when the GitHub feed is malformed or unsafe to apply."""


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


class PartialUpdateError(StopLigaError):
    """Raised when a multi-step remote update partially succeeds before failing."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
