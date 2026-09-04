"""Gunicorn hooks for Prometheus multiprocess worker lifecycle."""

from prometheus_client import multiprocess


def child_exit(_server, worker) -> None:
    """Remove metric files for a worker after Gunicorn has reaped it."""

    multiprocess.mark_process_dead(worker.pid)
