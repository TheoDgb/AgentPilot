"""Code commun de l'agent Support : lecture de l'horloge de ShopSim."""

from datetime import date
from typing import Any

import httpx
import pytest

from agentpilot import support_agent
from agentpilot.support_agent import AgentRunError

# Fonction réelle : la fixture automatique la remplace dans les autres tests
REAL_SIMULATION_TODAY = support_agent.simulation_today


def test_simulation_today_reads_the_shopsim_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        seen.append(url)
        return httpx.Response(
            200,
            json={"now": "2026-07-09T08:30:00Z", "frozen": False},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(support_agent.httpx, "get", fake_get)

    assert REAL_SIMULATION_TODAY("http://shopsim.test/") == date(2026, 7, 9)
    assert seen == ["http://shopsim.test/clock"]


def test_unreadable_clock_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(support_agent.httpx, "get", down)

    with pytest.raises(AgentRunError, match="Horloge de ShopSim illisible"):
        REAL_SIMULATION_TODAY("http://shopsim.test")
