"""Ligne de log `tool.call` par appel d'outil, commune aux serveurs MCP Support et Ops."""

import functools
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from observability.logs import error_type, log_event

Tool = Callable[..., Awaitable[Any]]


def logged(tool: Tool, logger: logging.Logger, fields: Mapping[str, str]) -> Tool:
    """Même outil (nom, description, paramètres), avec une ligne `tool.call` par appel ; seuls
    les arguments de `fields` sont écrits (jamais un texte libre), sous le nom de champ donné
    (un argument `service` ne doit pas écraser le champ `service` de la ligne)."""

    @functools.wraps(tool)
    async def wrapper(**arguments: Any) -> Any:  # noqa: ANN401
        values = {field: arguments[key] for key, field in fields.items() if key in arguments}
        start = time.perf_counter()

        def log_call(outcome: str, level: int, **extra: Any) -> None:  # noqa: ANN401
            duration_ms = round((time.perf_counter() - start) * 1000)
            log_event(
                logger,
                "tool.call",
                level,
                tool=tool.__name__,
                outcome=outcome,
                duration_ms=duration_ms,
                **values,
                **extra,
            )

        try:
            result = await tool(**arguments)
        except Exception as exc:
            log_call("error", logging.WARNING, error_type=error_type(exc))
            raise
        log_call("ok", logging.INFO)
        return result

    return wrapper
