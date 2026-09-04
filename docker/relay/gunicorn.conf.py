"""Gunicorn lifecycle hooks for relay metrics and privacy-safe logging."""

import logging

from prometheus_client import multiprocess


class _PrivacyFilter(logging.Filter):
    """Remove request-controlled values from Gunicorn error records."""

    def filter(self, record: logging.LogRecord) -> bool:
        template = str(record.msg)
        sanitized = True
        if template.startswith("Invalid request from ip="):
            record.msg = "gunicorn.invalid_request"
        elif template.startswith("Error handling request"):
            record.msg = "gunicorn.request_error"
        elif record.exc_info or record.exc_text:
            record.msg = "gunicorn.exception"
        else:
            sanitized = False

        if sanitized:
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def _install_privacy_filter(server) -> None:
    """Install one filter on the underlying ``gunicorn.error`` logger."""

    error_log = getattr(server, "error_log", None)
    if error_log is None:
        error_log = getattr(getattr(server, "log", None), "error_log", None)
    if error_log is None:
        return
    if not any(isinstance(item, _PrivacyFilter) for item in error_log.filters):
        error_log.addFilter(_PrivacyFilter())


def on_starting(server) -> None:
    """Sanitize master and inherited worker error logging before forking."""

    _install_privacy_filter(server)


def post_fork(server, worker) -> None:
    """Ensure workers retain the error-log privacy boundary after forking."""

    _install_privacy_filter(worker)
    _install_privacy_filter(server)


def child_exit(_server, worker) -> None:
    """Remove exited workers' live gauge shards from the dedicated directory."""

    multiprocess.mark_process_dead(worker.pid)
