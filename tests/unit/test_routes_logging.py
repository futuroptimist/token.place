import importlib
import importlib
import os
from unittest.mock import MagicMock, patch

import pytest


@patch.dict(os.environ, {"ENVIRONMENT": "dev"})
def test_log_info_dev(monkeypatch):
    import api.v1.routes as routes
    importlib.reload(routes)
    logger = MagicMock()
    monkeypatch.setattr(routes, 'logger', logger)
    routes.log_info('hi')
    logger.info.assert_called_once_with('hi')

@patch.dict(os.environ, {"ENVIRONMENT": "prod"})
def test_log_info_prod(monkeypatch):
    import api.v1.routes as routes
    importlib.reload(routes)
    logger = MagicMock()
    monkeypatch.setattr(routes, 'logger', logger)
    routes.log_info('hi')
    logger.info.assert_not_called()


@patch.dict(os.environ, {"ENVIRONMENT": "dev"})
def test_log_error_dev(monkeypatch):
    import api.v1.routes as routes
    importlib.reload(routes)
    logger = MagicMock()
    monkeypatch.setattr(routes, 'logger', logger)
    routes.log_error('oops')
    logger.error.assert_called_once_with('oops', exc_info=False)

@patch.dict(os.environ, {"ENVIRONMENT": "prod"})
def test_log_error_prod(monkeypatch):
    import api.v1.routes as routes
    importlib.reload(routes)
    logger = MagicMock()
    monkeypatch.setattr(routes, 'logger', logger)
    routes.log_error('oops')
    logger.error.assert_not_called()
