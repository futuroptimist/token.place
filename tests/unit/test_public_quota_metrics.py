"""Finite, privacy-safe public quota telemetry contract tests."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

from flask import Flask, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from api import (
    PUBLIC_QUOTA_METHODS,
    PUBLIC_QUOTA_OUTCOMES,
    PUBLIC_QUOTA_REASONS,
    PUBLIC_QUOTA_ROUTE_CLASSES,
    init_app,
)


def _app() -> tuple[Flask, CollectorRegistry]:
    registry = CollectorRegistry()
    app = Flask(__name__)
    init_app(
        app,
        metrics_registry=registry,
        metrics_export_defaults=False,
        metrics_path=None,
    )
    app.add_url_rule("/", endpoint="index", view_func=lambda: {"ok": True})
    app.add_url_rule(
        "/api/v1/meta", endpoint="api_v1_meta", view_func=lambda: {"ok": True}
    )
    app.add_url_rule(
        "/api/v1/version",
        endpoint="api_v1_version",
        view_func=lambda: {"ok": True},
    )
    return app, registry


def _samples(registry: CollectorRegistry) -> list[dict[str, str]]:
    families = text_string_to_metric_families(generate_latest(registry).decode())
    return [
        sample.labels
        for family in families
        for sample in family.samples
        if sample.name == "tokenplace_public_http_quota_outcomes_total"
    ]


@patch.dict(
    "os.environ", {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "100/day"}, clear=True
)
def test_accepted_and_hourly_limited_requests_are_independently_counted() -> None:
    app, registry = _app()
    with app.test_client() as client:
        assert client.get("/api/v1/models").status_code == 200
        assert client.get("/api/v1/models").status_code == 429

    labels = _samples(registry)
    assert {tuple(sorted(label.items())) for label in labels} == {
        tuple(
            sorted(
                {
                    "route_class": "api_v1",
                    "method": "GET",
                    "outcome": "accepted",
                    "reason": "none",
                }.items()
            )
        ),
        tuple(
            sorted(
                {
                    "route_class": "api_v1",
                    "method": "GET",
                    "outcome": "rejected",
                    "reason": "hourly_limit",
                }.items()
            )
        ),
    }


@patch.dict(
    "os.environ", {"API_RATE_LIMIT": "100/hour", "API_DAILY_QUOTA": "1/day"}, clear=True
)
def test_daily_limited_request_is_distinct() -> None:
    app, registry = _app()
    with app.test_client() as client:
        assert client.get("/api/v1/models").status_code == 200
        assert client.get("/api/v1/models").status_code == 429
    assert any(
        label["outcome"] == "rejected" and label["reason"] == "daily_limit"
        for label in _samples(registry)
    )


@patch.dict(
    "os.environ", {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "100/day"}, clear=True
)
def test_unknown_route_and_other_429_use_bounded_outcomes() -> None:
    app, registry = _app()
    app.add_url_rule(
        "/application-rejection",
        endpoint="application_rejection",
        view_func=lambda: ({"error": "bounded"}, 429),
    )
    with app.test_client() as client:
        assert client.get("/never-matched").status_code == 404

    unmatched = [
        label for label in _samples(registry) if label["route_class"] == "unmatched"
    ]
    assert {label["outcome"] for label in unmatched} == {"accepted"}
    assert {label["reason"] for label in unmatched} == {"none"}

    app, registry = _app()
    app.add_url_rule(
        "/application-rejection",
        endpoint="application_rejection",
        view_func=lambda: ({"error": "bounded"}, 429),
    )
    with app.test_client() as client:
        assert client.get("/application-rejection").status_code == 429
    assert any(
        label["route_class"] == "other_known" and label["reason"] == "other_rejection"
        for label in _samples(registry)
    )


@patch.dict(
    "os.environ", {"API_RATE_LIMIT": "1/hour", "API_DAILY_QUOTA": "1/day"}, clear=True
)
def test_public_information_get_and_head_are_exempt_by_stable_route_class() -> None:
    app, registry = _app()
    with app.test_client() as client:
        for method in (client.get, client.head):
            for path in ("/", "/api/v1/meta", "/api/v1/version"):
                assert method(path).status_code == 200
    labels = _samples(registry)
    assert {
        (label["route_class"], label["method"], label["outcome"]) for label in labels
    } == {
        (route_class, method, "exempt")
        for route_class in ("root", "public_metadata", "public_version")
        for method in ("GET", "HEAD")
    }


def test_metrics_scrapes_do_not_mutate_public_quota_counter() -> None:
    app, registry = _app()

    @app.get("/metrics", endpoint="metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)

    with app.test_client() as client:
        assert client.get("/api/v1/models").status_code == 200
        before_scrapes = generate_latest(registry)
        for _ in range(3):
            response = client.get("/metrics")
            assert response.status_code == 200

    assert generate_latest(registry) == before_scrapes
    assert all(label["route_class"] != "operational" for label in _samples(registry))


@patch.dict(
    "os.environ",
    {"API_RATE_LIMIT": "10000/hour", "API_DAILY_QUOTA": "10000/day"},
    clear=True,
)
def test_unmatched_paths_identities_and_attacker_values_cannot_add_series() -> None:
    app, registry = _app()
    sentinels = (
        "raw-path-sentinel",
        "query-secret-sentinel",
        "direct-address-sentinel",
        "forwarded-address-sentinel",
        "limiter-key-sentinel",
        "request-id-sentinel",
        "bearer-token-sentinel",
        "credential-sentinel",
        "exception-text-sentinel",
    )
    with app.test_client() as client:
        for index in range(2000):
            response = client.open(
                f"/{sentinels[0]}-{index}",
                method="UNRECOGNIZED",
                query_string={"value": sentinels[1]},
                environ_base={"REMOTE_ADDR": f"198.51.{index // 256}.{index % 256}"},
                headers={
                    "Forwarded": f"for={sentinels[3]}-{index}",
                    "X-Forwarded-For": f"203.0.113.{index % 256}",
                    "X-Limiter-Key": sentinels[4],
                    "X-Request-Id": sentinels[5],
                    "Authorization": f"Bearer {sentinels[6]}",
                    "X-Credential": sentinels[7],
                    "X-Exception": sentinels[8],
                },
            )
            assert response.status_code == 404

    exposition = generate_latest(registry).decode()
    assert len(_samples(registry)) == 1
    assert _samples(registry)[0] == {
        "route_class": "unmatched",
        "method": "other",
        "outcome": "accepted",
        "reason": "none",
    }
    assert all(sentinel not in exposition for sentinel in sentinels)


def test_label_vocabularies_and_cardinality_bound_are_closed() -> None:
    assert PUBLIC_QUOTA_ROUTE_CLASSES == (
        "root",
        "public_metadata",
        "public_version",
        "api_v1",
        "api_v2",
        "operational",
        "static",
        "control_plane",
        "other_known",
        "unmatched",
    )
    assert PUBLIC_QUOTA_METHODS == (
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "HEAD",
        "other",
    )
    assert PUBLIC_QUOTA_OUTCOMES == ("accepted", "exempt", "rejected")
    assert PUBLIC_QUOTA_REASONS == (
        "none",
        "hourly_limit",
        "daily_limit",
        "other_limit",
        "other_rejection",
    )
    assert (
        len(PUBLIC_QUOTA_ROUTE_CLASSES)
        * len(PUBLIC_QUOTA_METHODS)
        * len(PUBLIC_QUOTA_OUTCOMES)
        * len(PUBLIC_QUOTA_REASONS)
        == 1200
    )


def test_counter_serializes_in_canonical_single_worker_multiprocess_environment(
    tmp_path,
) -> None:
    """The image's multiprocess env must not suppress the supported worker's counter."""

    script = """
from flask import Flask
from prometheus_client import CollectorRegistry, generate_latest
from api import init_app
registry = CollectorRegistry()
app = Flask(__name__)
init_app(app, metrics_registry=registry, metrics_export_defaults=False, metrics_path=None)
with app.test_client() as client:
    assert client.get('/api/v1/models').status_code == 200
body = generate_latest(registry).decode()
assert 'tokenplace_public_http_quota_outcomes_total' in body
assert 'route_class="api_v1"' in body
"""
    environment = os.environ.copy()
    environment["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
