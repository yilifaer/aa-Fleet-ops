"""Safe reporting of unexpected errors.

Exception text from requests, urllib3, django-esi or an SRP provider can echo webhook
URLs, tokens or other secrets, so it is never stored, shown or logged. Only the exception
class and the traceback frames are kept. FleetESIError messages are written for users and
are kept as they are.
"""

import traceback

from fleetops.providers.esi import FleetESIError


def failure_message(step, exc):
    """Text that is safe to store and show for a failed step."""
    if isinstance(exc, FleetESIError):
        return str(exc)
    return f"{step} failed unexpectedly ({type(exc).__name__})."


def log_failure(logger, step, exc, operation=None):
    """Log an unexpected error with its class and traceback frames, but never its message."""
    target = f" for operation {operation.pk}" if getattr(operation, "pk", None) else ""
    frames = "".join(traceback.format_tb(exc.__traceback__)).rstrip()
    logger.error(
        "FleetOps: %s failed%s (%s).\nTraceback (most recent call last):\n%s",
        step,
        target,
        type(exc).__name__,
        frames,
    )


def report_failure(logger, step, exc, operation=None):
    """Log ``exc`` unless it is a user-facing FleetESIError and return the text to store."""
    if not isinstance(exc, FleetESIError):
        log_failure(logger, step, exc, operation)
    return failure_message(step, exc)
