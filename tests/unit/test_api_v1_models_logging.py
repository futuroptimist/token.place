import importlib
import os
from unittest.mock import MagicMock, patch

import pytest

@patch.dict(os.environ, {"ENVIRONMENT": "dev"})
def test_log_functions_call(monkeypatch):
    import api.v1.models as models
    importlib.reload(models)
    fake_logger = MagicMock()
    monkeypatch.setattr(models, "logger", fake_logger)
    models.log_info("info")
    models.log_warning("warn")
    models.log_error("err", exc_info=True)
    fake_logger.info.assert_called_with("info")
    fake_logger.warning.assert_called_with("warn")
    fake_logger.error.assert_called_with("err", exc_info=True)
