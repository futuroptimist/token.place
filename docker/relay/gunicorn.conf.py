"""Gunicorn lifecycle hooks for relay Prometheus multiprocess state."""

from prometheus_client import multiprocess


def child_exit(_server, worker) -> None:
    """Remove exited workers' live gauge shards from the dedicated directory."""

    multiprocess.mark_process_dead(worker.pid)
