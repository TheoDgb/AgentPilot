"""Canal Discord (J7.2) : message de fin d'enquête, sans aucun envoi réel (faux `post`)."""

import io
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from agentpilot import metrics, notify
from agentpilot.notify import Investigation, Notifier
from agentpilot.ops_schemas import Confidence, IncidentCause
from agentpilot.support_agent import REPO_ROOT
from observability.logs import configure, register_secret

pytestmark = pytest.mark.anyio

WEBHOOK = "https://discord.com/api/webhooks/123456789/token-of-the-tests-abcdef"
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
RUN_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
REPORT = {
    "probable_cause": "database_errors",
    "confidence": "high",
    # Textes libres de l'agent : jamais dans le message
    "summary": "Erreurs de base ; le client jean.dupont@example.com se plaint",
    "proposed_fix": "Redémarrer la base de Jean Dupont",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adresse dans l'environnement, rien de capturé d'un test précédent."""
    monkeypatch.setattr(notify, "_captured", None)
    monkeypatch.setattr(notify, "_warned", False)
    monkeypatch.setenv(notify.WEBHOOK_ENV, WEBHOOK)


class FakePost:
    def __init__(self, status: int = 204, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, body: dict[str, Any]) -> int:
        self.calls.append((url, body))
        if self.error is not None:
            raise self.error
        return self.status


def succeeded(covered: int = 1) -> Investigation:
    return Investigation.from_output(
        RUN_ID, "ShopSimHighErrorRate", "/orders/{order_id}", covered, REPORT, None
    )


def logged() -> io.StringIO:
    """Logs JSON du module, dans un flux du test."""
    stream = io.StringIO()
    configure("agentpilot", None, logger_name="agentpilot.notify", stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def sent_count() -> float:
    return (
        metrics.EVENTS.get_sample_value("agentpilot_notifications_total", {"result": "sent"}) or 0
    )


# --- Contenu -----------------------------------------------------------------------------------


async def test_finished_investigation_message() -> None:
    post = FakePost()
    before = sent_count()

    result = await Notifier(post).investigation_finished(succeeded(), NOW)

    assert result == "sent" and sent_count() == before + 1
    [(url, body)] = post.calls
    assert url == WEBHOOK
    assert body["content"] == (
        "🔎 Enquête terminée — ShopSimHighErrorRate sur /orders/{order_id} (+1 alerte couverte)\n"
        "Cause probable : erreurs de la base de données · confiance : élevée\n"
        f"http://localhost:8001/investigations/{RUN_ID}"
    )
    # Jamais de mention de tout le salon
    assert body["allowed_mentions"] == {"parse": []}


async def test_message_never_carries_free_text_of_the_agent() -> None:
    post = FakePost()

    await Notifier(post).investigation_finished(succeeded(), NOW)

    content = post.calls[0][1]["content"]
    assert "jean.dupont" not in content and "Dupont" not in content and "@" not in content


async def test_failed_investigation_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.PUBLIC_URL_ENV, "http://agentpilot.lan:8001/")
    post = FakePost()
    found = Investigation.from_output(RUN_ID, "ShopSimDown", None, 0, None, "invalid_output")

    await Notifier(post).investigation_finished(found, NOW)

    assert post.calls[0][1]["content"] == (
        "⚠️ Enquête en échec (invalid_output) — ShopSimDown\n"
        "Rapport à lire par un humain.\n"
        f"http://agentpilot.lan:8001/investigations/{RUN_ID}"
    )


def test_only_enumerated_values_are_read_from_the_report() -> None:
    found = Investigation.from_output(
        RUN_ID, "X", None, 0, {"probable_cause": "rm -rf /", "confidence": "high"}, None
    )

    assert (found.cause, found.confidence) == (None, None)
    ok = succeeded()
    assert (ok.cause, ok.confidence) == (IncidentCause.DATABASE_ERRORS, Confidence.HIGH)
    assert set(notify.CAUSES) == set(IncidentCause)
    assert set(notify.CONFIDENCES) == set(Confidence)


# --- Adresse -----------------------------------------------------------------------------------


async def test_without_address_nothing_is_sent_and_nothing_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(notify.WEBHOOK_ENV)
    stream = logged()
    post = FakePost()

    assert await Notifier(post).investigation_finished(succeeded(), NOW) is None

    assert post.calls == [] and stream.getvalue() == ""
    assert notify.configured() is False


async def test_an_address_of_another_site_is_refused_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.com/hook?token=leak-me-please")
    stream = logged()
    post = FakePost()
    notifier = Notifier(post)

    await notifier.investigation_finished(succeeded(), NOW)
    await notifier.investigation_finished(succeeded(), NOW)

    assert post.calls == []
    [warning] = lines(stream)
    assert warning["event"] == "notify.disabled" and "leak-me-please" not in stream.getvalue()


def test_capture_takes_the_address_out_of_the_environment() -> None:
    environ = {notify.WEBHOOK_ENV: WEBHOOK, "PATH": "/bin"}

    notify.capture(environ)

    assert environ == {"PATH": "/bin"}
    assert notify.webhook_url() == WEBHOOK


def test_address_is_never_in_the_repository() -> None:
    tracked = subprocess.run(
        ["git", "grep", "-lE", r"discord(app)?\.com/api/webhooks/[0-9]{6,}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # Seuls les tests citent une fausse adresse
    assert [f for f in tracked.stdout.split() if "/tests/" not in f] == []


# --- Envoi -------------------------------------------------------------------------------------


async def test_hourly_cap_drops_the_extra_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(notify.MAX_PER_HOUR_ENV, "2")
    post = FakePost()
    notifier = Notifier(post)

    results = [
        await notifier.investigation_finished(succeeded(), NOW + timedelta(minutes=m))
        for m in (0, 10, 20)
    ]
    # Une heure après le premier, une place se libère
    later = await notifier.investigation_finished(succeeded(), NOW + timedelta(minutes=61))

    assert results == ["sent", "sent", "dropped"] and later == "sent"
    assert len(post.calls) == 3


@pytest.mark.parametrize(
    "post",
    [FakePost(status=429), FakePost(error=httpx.ConnectTimeout("délai"))],
    ids=["rate-limited", "timeout"],
)
async def test_a_failure_is_logged_without_the_address(post: FakePost) -> None:
    register_secret(WEBHOOK)
    stream = logged()

    assert await Notifier(post).investigation_finished(succeeded(), NOW) == "failed"

    [line] = lines(stream)
    assert line["event"] == "notify.failed" and line["level"] == "error"
    assert "token-of-the-tests" not in stream.getvalue()
