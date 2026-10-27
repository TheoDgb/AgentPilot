"""Réglages communs à tous les tests."""

import logging
from collections.abc import Iterator

import pytest

from observability import logs


@pytest.fixture(autouse=True)
def strict_log_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un champ de log hors liste blanche fait échouer le test au lieu d'être ignoré."""
    monkeypatch.setattr(logs, "STRICT", True)


@pytest.fixture(autouse=True)
def restore_loggers() -> Iterator[None]:
    """Un test qui configure les logs (lifespan, `configure`) ne laisse rien aux suivants."""
    loggers = [logging.getLogger()] + [
        logger
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    saved = {
        logger: (list(logger.handlers), logger.level, logger.propagate, logger.disabled)
        for logger in loggers
    }
    log_dir = logs.log_dir()
    yield
    for logger in [logging.getLogger()] + [
        logger
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]:
        handlers, level, propagate, disabled = saved.get(logger, ([], logging.NOTSET, True, False))
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
    logs._log_dir = log_dir
