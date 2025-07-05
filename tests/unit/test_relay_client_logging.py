from unittest.mock import MagicMock
import utils.networking.relay_client as rc


def test_log_info_respects_config(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(rc, 'logger', logger)
    config = MagicMock(is_production=False)
    monkeypatch.setattr(rc, 'get_config_lazy', lambda: config)
    rc.log_info('Hi {}', 'there')
    logger.info.assert_called_with('Hi there')
    logger.info.reset_mock()
    config.is_production = True
    rc.log_info('No log')
    logger.info.assert_not_called()


def test_log_info_fallback(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(rc, 'logger', logger)
    def boom():
        raise Exception('fail')
    monkeypatch.setattr(rc, 'get_config_lazy', boom)
    rc.log_info('Hello {}', 'world')
    logger.info.assert_called_with('Hello world')


def test_log_error_respects_config(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(rc, 'logger', logger)
    config = MagicMock(is_production=False)
    monkeypatch.setattr(rc, 'get_config_lazy', lambda: config)
    rc.log_error('Err {}', 'oops')
    logger.error.assert_called_with('Err oops', exc_info=False)
    logger.error.reset_mock()
    config.is_production = True
    rc.log_error('Silence')
    logger.error.assert_not_called()


def test_log_error_fallback(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(rc, 'logger', logger)
    def boom():
        raise Exception('fail')
    monkeypatch.setattr(rc, 'get_config_lazy', boom)
    rc.log_error('Bad {}', 'news', exc_info=True)
    logger.error.assert_called_with('Bad news', exc_info=True)
