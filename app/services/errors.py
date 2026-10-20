"""Service-level errors mapped to HTTP responses by the API layer."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.__doc__ or self.code
        super().__init__(self.message)


class NotFoundError(ServiceError):
    """The requested resource does not exist."""

    status_code = 404
    code = "not_found"


class UnauthorizedError(ServiceError):
    """Missing or invalid API key."""

    status_code = 401
    code = "unauthorized"


class ConflictError(ServiceError):
    """The resource is not in a state that allows this operation."""

    status_code = 409
    code = "conflict"


class ValidationFailed(ServiceError):
    """The request was well-formed but semantically invalid."""

    status_code = 422
    code = "validation_failed"


class ProviderUnavailable(ServiceError):
    """The upstream payment provider could not be reached."""

    status_code = 502
    code = "provider_unavailable"
