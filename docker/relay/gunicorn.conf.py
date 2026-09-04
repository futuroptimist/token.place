"""Gunicorn hooks for the relay's Prometheus multiprocess lifecycle."""

from prometheus_client import multiprocess


def child_exit(server, worker):
    """Remove files for workers that have exited after Gunicorn has reaped them."""

    multiprocess.mark_process_dead(worker.pid)
