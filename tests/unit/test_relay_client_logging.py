from unittest.mock import MagicMock, patch
import utils.networking.relay_client as rc


def test_log_info_non_production():
    logger = MagicMock()
    cfg = MagicMock(is_production=False)
    with patch.object(rc, 'logger', logger), patch.object(rc, 'get_config_lazy', return_value=cfg):
        rc.log_info("hello {}", "world")
    logger.info.assert_called_with("hello world")


def test_log_info_production():
    logger = MagicMock()
    cfg = MagicMock(is_production=True)
    with patch.object(rc, 'logger', logger), patch.object(rc, 'get_config_lazy', return_value=cfg):
        rc.log_info("ignored")
    logger.info.assert_not_called()


def test_log_info_fallback():
    logger = MagicMock()
    with patch.object(rc, 'logger', logger), patch.object(rc, 'get_config_lazy', side_effect=RuntimeError()):
        rc.log_info("hi {}", "there")
    logger.info.assert_called_with("hi there")


def test_log_error_non_production():
    logger = MagicMock()
    cfg = MagicMock(is_production=False)
    with patch.object(rc, 'logger', logger), patch.object(rc, 'get_config_lazy', return_value=cfg):
        rc.log_error("err {}", "msg", exc_info=True)
    logger.error.assert_called_with("err msg", exc_info=True)


def test_log_error_production():
    logger = MagicMock()
    cfg = MagicMock(is_production=True)
    with patch.object(rc, 'logger', logger), patch.object(rc, 'get_config_lazy', return_value=cfg):
        rc.log_error("ignored")
    logger.error.assert_not_called()


def test_log_error_fallback():
    logger = MagicMock()
    with patch.object(rc, 'logger', logger), patch.object(rc, 'get_config_lazy', side_effect=RuntimeError()):
        rc.log_error("oops {}", "fail")
    logger.error.assert_called_with("oops fail", exc_info=False)
