"""Format des logs JSON, trace, filet anti-secret et lecture des fichiers."""

import asyncio
import io
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from observability import logs, query, trace
from observability.logs import configure, configure_isolated, log_event, register_secret
from observability.query import main, read_logs, recent_lines

REQUIRED = {"ts", "level", "service", "event", "trace_id", "logger"}
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"

logger = logging.getLogger("observability.tests")


def lines(text: str) -> list[dict[str, Any]]:
    """Chaque ligne non vide est un objet JSON complet."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def stream() -> io.StringIO:
    out = io.StringIO()
    configure("test", logger_name="observability.tests", stream=out)
    return out


def test_each_event_is_one_json_line_with_the_required_fields(stream: io.StringIO) -> None:
    with trace.bind(trace_id=TRACE, run_id=uuid.UUID(int=1), ticket_id=7):
        log_event(logger, "run.started", trigger="loop")
    log_event(logger, "loop.paused", logging.WARNING, pause_reason="budget")

    first, second = lines(stream.getvalue())
    assert REQUIRED <= first.keys()
    assert first["service"] == "test"
    assert first["event"] == "run.started"
    assert first["level"] == "info"
    assert first["trace_id"] == TRACE
    assert first["run_id"] == str(uuid.UUID(int=1))
    assert first["ticket_id"] == 7
    assert first["trigger"] == "loop"
    assert datetime.fromisoformat(first["ts"]).tzinfo is not None
    # Hors trace : trace_id nul, contexte retiré
    assert second["trace_id"] is None
    assert "run_id" not in second
    assert second["level"] == "warning"


def test_ordinary_log_records_are_json_too(stream: io.StringIO) -> None:
    logger.info("Démarrage %s", "ok")

    (line,) = lines(stream.getvalue())
    assert line["event"] == "log"
    assert line["message"] == "Démarrage ok"


def test_exception_has_type_message_and_traceback(stream: io.StringIO) -> None:
    try:
        raise KeyError("REFUND_PROVIDER_URL")
    except KeyError:
        log_event(logger, "loop.error", logging.ERROR, exc_info=True)

    (line,) = lines(stream.getvalue())
    assert line["error_type"] == "KeyError"
    assert "REFUND_PROVIDER_URL" in line["error"]
    assert "Traceback" in line["traceback"]


def test_unknown_field_is_refused_in_tests_and_dropped_in_production(
    stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="body"):
        log_event(logger, "tool.called", body="texte du client")

    monkeypatch.setattr(logs, "STRICT", False)
    log_event(logger, "tool.called", tool="reply_to_ticket", body="texte du client")
    (line,) = lines(stream.getvalue())
    assert line["tool"] == "reply_to_ticket"
    assert "body" not in line
    assert "texte du client" not in stream.getvalue()


def test_secrets_are_redacted_everywhere_in_the_line(
    stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAOS_TOKEN", "chaos-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-from-environment")
    register_secret("key-read-from-dotenv")

    logger.info("jeton chaos-secret-value et key-from-environment")
    log_event(logger, "loop.backoff", reason="clé key-read-from-dotenv")
    try:
        raise RuntimeError("sk-ant-api03-AbC_d-123 refusée")
    except RuntimeError:
        logger.exception("échec")

    out = stream.getvalue()
    for secret in (
        "chaos-secret-value",
        "key-from-environment",
        "key-read-from-dotenv",
        "sk-ant-api03",
    ):
        assert secret not in out
    assert out.count(logs.REDACTED) >= 4
    assert all(lines(out))


def test_traceparent_parsing() -> None:
    header = trace.traceparent(TRACE)

    assert trace.parse_traceparent(header) == TRACE
    assert trace.parse_traceparent(header.upper()) == TRACE
    assert trace.parse_traceparent(None) is None
    assert trace.parse_traceparent("") is None
    assert trace.parse_traceparent("n'importe quoi") is None
    assert trace.parse_traceparent(f"00-{'0' * 32}-{'1' * 16}-01") is None
    assert trace.parse_traceparent(f"00-{TRACE}-short-01") is None
    assert len(trace.new_trace_id()) == 32


def test_headers_carry_the_current_trace() -> None:
    assert trace.headers() == {}
    with trace.bind(trace_id=TRACE):
        assert trace.parse_traceparent(trace.headers()["traceparent"]) == TRACE
    assert trace.current_trace_id() is None


def test_bind_refuses_unknown_context() -> None:
    with pytest.raises(ValueError, match="customer_message"), trace.bind(customer_message="x"):
        pass


@pytest.mark.anyio
async def test_parallel_tasks_keep_their_own_trace(stream: io.StringIO) -> None:
    async def run(trace_id: str, ticket_id: int) -> None:
        with trace.bind(trace_id=trace_id, ticket_id=ticket_id):
            for _ in range(3):
                await asyncio.sleep(0)
                await asyncio.to_thread(log_event, logger, "tool.called", tool="get_ticket")

    traces = {1: trace.new_trace_id(), 2: trace.new_trace_id()}
    await asyncio.gather(run(traces[1], 1), run(traces[2], 2))

    found = lines(stream.getvalue())
    assert len(found) == 6
    for line in found:
        assert line["trace_id"] == traces[line["ticket_id"]]


def test_service_file_and_stderr(tmp_path: Path) -> None:
    out = io.StringIO()
    configure("test", tmp_path, logger_name="observability.tests", stream=out)

    log_event(logger, "loop.resumed")

    assert lines(out.getvalue())[0]["event"] == "loop.resumed"
    assert lines((tmp_path / "test.jsonl").read_text())[0]["event"] == "loop.resumed"


def test_isolated_logger_writes_only_its_file(tmp_path: Path, stream: io.StringIO) -> None:
    configure_isolated("observability.tests.private", "test", tmp_path, "private.jsonl")
    private = logging.getLogger("observability.tests.private")

    log_event(private, "http.request", logging.WARNING, status=403)

    assert stream.getvalue() == ""
    assert lines((tmp_path / "private.jsonl").read_text())[0]["status"] == 403
    assert not (tmp_path / "test.jsonl").exists()


def test_isolated_logger_without_directory_writes_nowhere(
    tmp_path: Path, stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_isolated("observability.tests.private", "test", None, "private.jsonl")

    log_event(logging.getLogger("observability.tests.private"), "http.request", logging.ERROR)

    assert stream.getvalue() == ""
    # Pas même le gestionnaire de dernier recours de `logging`, qui écrit sur stderr
    assert capsys.readouterr().err == ""


# --- Lecture (base de l'outil de l'agent Ops) --------------------------------------------


def write(path: Path, *entries: dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def entry(minutes_ago: int, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    ts = datetime(2026, 7, 6, 12, tzinfo=UTC) - timedelta(minutes=minutes_ago)
    return {"ts": ts.isoformat().replace("+00:00", "Z"), "level": "info", **fields}


def test_read_logs_filters(tmp_path: Path) -> None:
    write(
        tmp_path / "shopsim.jsonl",
        entry(30, event="http.request", route="/orders/{order_id}", status=200, trace_id="a"),
        entry(10, event="http.request", route="/orders/{order_id}", status=500, level="error"),
        entry(5, event="http.request", route="/refunds", status=500, level="error"),
        entry(1, event="http.request", route="/refunds", status=201, trace_id="a"),
    )
    path = tmp_path / "shopsim.jsonl"
    with path.open("a") as file:
        file.write('{"ts": "incomplet')

    since = datetime(2026, 7, 6, 11, 45, tzinfo=UTC)
    assert len(read_logs(tmp_path, "shopsim")) == 4
    assert len(read_logs(tmp_path, "shopsim", since=since)) == 3
    assert [e["route"] for e in read_logs(tmp_path, "shopsim", status_min=500)] == [
        "/orders/{order_id}",
        "/refunds",
    ]
    assert len(read_logs(tmp_path, "shopsim", level="error")) == 2
    assert len(read_logs(tmp_path, "shopsim", trace_id="a")) == 2
    assert len(read_logs(tmp_path, "shopsim", route="/refunds")) == 2
    # Les plus récentes, dans l'ordre du fichier
    assert [e["status"] for e in read_logs(tmp_path, "shopsim", limit=2)] == [500, 201]
    assert read_logs(tmp_path, "mcp") == []


@pytest.mark.parametrize("name", ["chaos", "../shopsim", "/etc/passwd", "shopsim.jsonl", ""])
def test_read_logs_accepts_only_the_fixed_list_of_services(tmp_path: Path, name: str) -> None:
    write(tmp_path / "chaos.jsonl", entry(1, event="http.request", path="/chaos/db_errors"))

    with pytest.raises(ValueError, match="service inconnu"):
        read_logs(tmp_path, name)


def test_reading_from_the_end_gives_the_same_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fenêtre lue depuis la fin (petits blocs : des lignes coupées entre deux blocs) : mêmes
    lignes, dans le même ordre, que la lecture complète filtrée."""
    monkeypatch.setattr(query, "BLOCK_SIZE", 97)
    entries = [
        entry(90 - n, event="http.request", status=500 if n % 7 == 0 else 200, n=n)
        for n in range(90)
    ]
    write(tmp_path / "shopsim.jsonl", *entries)
    since = datetime(2026, 7, 6, 12, tzinfo=UTC) - timedelta(minutes=40)

    tail = read_logs(tmp_path, "shopsim", since=since, limit=200)
    full = [e for e in read_logs(tmp_path, "shopsim", limit=200) if e["ts"] >= since.isoformat()]

    assert [e["n"] for e in tail] == [e["n"] for e in full] == list(range(50, 90))
    assert [e["n"] for e in read_logs(tmp_path, "shopsim", since=since, limit=3)] == [87, 88, 89]
    assert [e["n"] for e in read_logs(tmp_path, "shopsim", since=since, status_min=500)] == [
        56,
        63,
        70,
        77,
        84,
    ]


def test_reading_from_the_end_keeps_slightly_unordered_lines(tmp_path: Path) -> None:
    """Plusieurs serveurs MCP écrivent dans `mcp.jsonl` : une ligne un peu plus ancienne écrite
    après une plus récente reste lue tant qu'elle est dans la marge (`ORDER_MARGIN`)."""
    write(
        tmp_path / "mcp.jsonl",
        entry(20, event="old"),
        entry(9, event="in_window"),
        entry(11, event="just_before_window"),  # écrite après, mais avant la fenêtre
        entry(8, event="late_but_in_window"),
        entry(10, event="written_late"),  # dans la fenêtre, après une ligne plus récente
    )
    since = datetime(2026, 7, 6, 12, tzinfo=UTC) - timedelta(minutes=10)

    events = [e["event"] for e in recent_lines(tmp_path, "mcp", since)]

    assert events == ["written_late", "late_but_in_window", "in_window"]


def test_a_symbolic_link_is_never_read(tmp_path: Path) -> None:
    write(tmp_path / "chaos.jsonl", entry(1, event="http.request", path="/chaos/db_errors"))
    (tmp_path / "shopsim.jsonl").symlink_to(tmp_path / "chaos.jsonl")

    with pytest.raises(ValueError, match="lien symbolique"):
        read_logs(tmp_path, "shopsim")
    with pytest.raises(ValueError, match="lien symbolique"):
        list(recent_lines(tmp_path, "shopsim", datetime(2026, 7, 6, tzinfo=UTC)))


# --- Ligne de commande --------------------------------------------------------------------


def test_command_follows_a_trace_across_services(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        tmp_path / "agentpilot.jsonl",
        entry(4, service="agentpilot", event="run.started", trace_id="t1", trigger="loop"),
        entry(1, service="agentpilot", event="run.finished", trace_id="t1", status="succeeded"),
    )
    write(
        tmp_path / "mcp.jsonl",
        entry(3, service="mcp", event="tool.call", trace_id="t1", tool="get_ticket"),
    )
    write(
        tmp_path / "shopsim.jsonl",
        entry(2, service="shopsim", event="http.request", trace_id="t1", path="/tickets/4"),
        entry(2, service="shopsim", event="http.request", trace_id="t2", path="/clock"),
    )
    write(tmp_path / "chaos.jsonl", entry(2, event="http.request", trace_id="t1"))

    assert main(["--dir", str(tmp_path), "--trace", "t1"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert [line.split()[1:4] for line in out] == [
        ["agentpilot", "info", "run.started"],
        ["mcp", "info", "tool.call"],
        ["shopsim", "info", "http.request"],
        ["agentpilot", "info", "run.finished"],
    ]
    assert out[0].endswith("trigger=loop")
    assert "path=/tickets/4" in out[2]


def test_command_json_output_and_service_filter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        tmp_path / "shopsim.jsonl",
        entry(2, event="http.exception", error="server closed the connection", level="error"),
        entry(1, event="http.request", status=200),
    )

    assert main(["--dir", str(tmp_path), "--service", "shopsim", "--level", "error"]) == 0
    assert 'error="server closed the connection"' in capsys.readouterr().out

    assert main(["--dir", str(tmp_path), "--service", "shopsim", "--json", "--limit", "1"]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["status"] == 200


def test_command_refuses_other_files_and_says_when_nothing_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["--dir", str(tmp_path), "--service", "chaos"])

    assert main(["--dir", str(tmp_path), "--trace", "absent"]) == 1
    assert "Aucune ligne" in capsys.readouterr().err


def test_configured_directory_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    configure("test", "relative-logs", stream=None)

    assert logs.log_dir() == tmp_path / "relative-logs"


def test_command_hides_empty_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(
        tmp_path / "agentpilot.jsonl",
        entry(1, event="run.finished", status="succeeded", error_type=None, action_id=None),
    )

    assert main(["--dir", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "status=succeeded" in out
    assert "None" not in out
