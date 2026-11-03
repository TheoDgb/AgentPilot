"""Rotation des logs (J7.1) : un fichier par jour UTC, jamais renommé, sûr avec plusieurs
processus ; conservation ; lecture d'une fenêtre à cheval sur deux fichiers."""

import json
import logging
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from mcp_server.ops import common
from mcp_server.ops.logs_tools import search_logs, summarize_logs
from observability.logs import (
    DailyFileHandler,
    JsonFormatter,
    purge,
    read_lines,
    retention_days,
    segments,
)
from observability.query import log_files, read_logs, recent_lines

pytestmark = pytest.mark.anyio
MIDNIGHT = datetime(2026, 11, 4, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def record(at: datetime, event: str = "x") -> logging.LogRecord:
    found = logging.LogRecord("test", logging.INFO, __file__, 1, event, None, None)
    found.created = at.timestamp()
    found.fields = {}  # type: ignore[attr-defined]
    return found


def handler(tmp_path: Path, stem: str = "shopsim") -> DailyFileHandler:
    found = DailyFileHandler(tmp_path, stem)
    found.setFormatter(JsonFormatter(stem))
    return found


def line(at: datetime, **fields: object) -> str:
    stamp = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return json.dumps({"ts": stamp, "level": "info", "event": "http.request", **fields}) + "\n"


# --- Écriture -----------------------------------------------------------------------------------


def test_each_line_goes_to_the_file_of_its_day(tmp_path: Path) -> None:
    write = handler(tmp_path)
    for at, event in (
        (MIDNIGHT - timedelta(seconds=2), "before"),
        (MIDNIGHT + timedelta(seconds=1), "after"),
    ):
        write.handle(record(at, event))
    write.close()

    assert [p.name for _, p in segments(tmp_path, "shopsim")] == [
        "shopsim-2026-11-03.jsonl",
        "shopsim-2026-11-04.jsonl",
    ]
    assert [json.loads(x)["event"] for x in read_lines(tmp_path, "shopsim.jsonl")] == [
        "before",
        "after",
    ]


def test_a_symlinked_day_file_is_never_written_through(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("")
    (tmp_path / "shopsim-2026-11-04.jsonl").symlink_to(target)
    write = handler(tmp_path)

    write.handle(record(MIDNIGHT))
    write.close()

    assert target.read_text() == ""


WRITER = """
import logging, sys
from observability.logs import configure_isolated, log_event
configure_isolated("w", "mcp", sys.argv[1], "mcp.jsonl")
logger = logging.getLogger("w")
for n in range(int(sys.argv[2])):
    log_event(logger, "tool.call", tool="x" * 300, count=n)
"""


def test_several_processes_write_the_same_file_without_losing_a_line(tmp_path: Path) -> None:
    """Plusieurs serveurs MCP écrivent dans le même fichier du jour en même temps."""
    count = 400
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    writers = [
        subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), str(count)], env=env)
        for _ in range(4)
    ]
    for process in writers:
        assert process.wait(timeout=60) == 0

    found = read_lines(tmp_path, "mcp.jsonl")
    assert len(found) == 4 * count
    assert all(json.loads(x)["event"] == "tool.call" for x in found)  # aucune ligne coupée


def test_retention_removes_only_old_files_of_its_own_name(tmp_path: Path) -> None:
    today = date(2026, 11, 4)
    for name in (
        "shopsim-2026-10-27.jsonl",  # 8 jours : supprimé
        "shopsim-2026-10-28.jsonl",  # 7 jours : gardé
        "shopsim-2026-11-03.jsonl",
        "chaos-2026-10-01.jsonl",  # autre nom : jamais touché ici
        "ops_shopsim-2026-10-01.jsonl",
    ):
        (tmp_path / name).write_text("{}\n")
    old = tmp_path / "shopsim.jsonl"
    old.write_text("{}\n")
    stale = (datetime(2026, 10, 1, tzinfo=UTC)).timestamp()
    os.utime(old, (stale, stale))

    purge(tmp_path, "shopsim", today, 7)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "chaos-2026-10-01.jsonl",
        "ops_shopsim-2026-10-01.jsonl",
        "shopsim-2026-10-28.jsonl",
        "shopsim-2026-11-03.jsonl",
    ]


def test_retention_runs_when_a_new_day_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_RETENTION_DAYS", "1")
    (tmp_path / "shopsim-2026-11-01.jsonl").write_text("{}\n")
    write = handler(tmp_path)

    write.handle(record(MIDNIGHT))
    write.close()

    assert [p.name for _, p in segments(tmp_path, "shopsim")] == ["shopsim-2026-11-04.jsonl"]


def test_recent_legacy_file_is_kept(tmp_path: Path) -> None:
    (tmp_path / "shopsim.jsonl").write_text("{}\n")

    purge(tmp_path, "shopsim", datetime.now(UTC).date(), 7)

    assert (tmp_path / "shopsim.jsonl").exists()


# --- Lecture ------------------------------------------------------------------------------------


def two_days(tmp_path: Path) -> None:
    (tmp_path / "shopsim-2026-11-03.jsonl").write_text(
        line(MIDNIGHT - timedelta(minutes=30), n=1) + line(MIDNIGHT - timedelta(minutes=2), n=2)
    )
    (tmp_path / "shopsim-2026-11-04.jsonl").write_text(
        line(MIDNIGHT + timedelta(minutes=1), n=3) + line(MIDNIGHT + timedelta(minutes=3), n=4)
    )


def test_window_across_midnight_reads_both_files(tmp_path: Path) -> None:
    two_days(tmp_path)

    found = list(recent_lines(tmp_path, "shopsim", MIDNIGHT - timedelta(minutes=5)))

    assert [x["n"] for x in found] == [4, 3, 2]


def test_window_inside_today_does_not_read_yesterday(tmp_path: Path) -> None:
    two_days(tmp_path)
    # Hier illisible : il ne doit pas être ouvert
    (tmp_path / "shopsim-2026-11-03.jsonl").chmod(0)

    found = list(recent_lines(tmp_path, "shopsim", MIDNIGHT + timedelta(minutes=2)))

    assert [x["n"] for x in found] == [4]
    (tmp_path / "shopsim-2026-11-03.jsonl").chmod(0o644)


def test_all_lines_in_order_without_window(tmp_path: Path) -> None:
    two_days(tmp_path)
    (tmp_path / "shopsim.jsonl").write_text(line(MIDNIGHT - timedelta(days=2), n=0))

    assert [x["n"] for x in read_logs(tmp_path, "shopsim")] == [0, 1, 2, 3, 4]


def test_legacy_file_is_still_read_after_the_new_files(tmp_path: Path) -> None:
    (tmp_path / "shopsim.jsonl").write_text(line(MIDNIGHT - timedelta(minutes=10), n=0))
    (tmp_path / "shopsim-2026-11-04.jsonl").write_text(line(MIDNIGHT + timedelta(minutes=1), n=1))

    found = list(recent_lines(tmp_path, "shopsim", MIDNIGHT - timedelta(minutes=20)))

    assert [x["n"] for x in found] == [1, 0]


def test_chaos_and_other_names_are_never_read(tmp_path: Path) -> None:
    (tmp_path / "chaos-2026-11-04.jsonl").write_text(line(MIDNIGHT, n=9))
    (tmp_path / "ops_mcp-2026-11-04.jsonl").write_text(line(MIDNIGHT, n=9))
    (tmp_path / "mcp-extra-2026-11-04.jsonl").write_text(line(MIDNIGHT, n=9))

    assert log_files(tmp_path, "mcp") == []
    with pytest.raises(ValueError, match="service inconnu"):
        log_files(tmp_path, "chaos")


def test_a_symlinked_day_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "chaos-2026-11-04.jsonl").write_text(line(MIDNIGHT, n=9))
    (tmp_path / "shopsim-2026-11-04.jsonl").symlink_to(tmp_path / "chaos-2026-11-04.jsonl")

    with pytest.raises(ValueError, match="lien symbolique"):
        list(recent_lines(tmp_path, "shopsim", MIDNIGHT - timedelta(hours=1)))


async def test_ops_log_tools_see_a_window_across_two_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'agent Ops lit les 10 dernières minutes à 00:03 : la veille et le jour même."""
    (tmp_path / "shopsim-2026-11-03.jsonl").write_text(
        line(MIDNIGHT - timedelta(minutes=4), route="/orders/{order_id}", status=500)
    )
    (tmp_path / "shopsim-2026-11-04.jsonl").write_text(
        line(MIDNIGHT + timedelta(minutes=2), route="/orders/{order_id}", status=200)
    )
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(common, "clock", lambda: MIDNIGHT + timedelta(minutes=3))

    summary = await summarize_logs("shopsim", 10)
    found = await search_logs("shopsim", since_minutes=10)

    [route] = summary["requests"]["routes"]
    assert (route["count"], route["5xx"]) == (2, 1)
    assert found["matched"] == 2


def test_handler_writes_one_line_per_write_call(tmp_path: Path) -> None:
    """Une ligne longue part en un seul `write` (pas de mélange avec un autre processus)."""
    calls: list[int] = []
    real = os.write

    def counting(fd: int, data: bytes) -> int:
        calls.append(len(data))
        return real(fd, data)

    write = handler(tmp_path)
    original, os.write = os.write, counting  # type: ignore[assignment]
    try:
        big = record(MIDNIGHT, "x" * 20_000)
        write.handle(big)
    finally:
        os.write = original  # type: ignore[assignment]
        write.close()

    assert len(calls) == 1 and calls[0] > 20_000


@pytest.mark.parametrize("value", ["sept", "0", "-3"])
def test_invalid_retention_keeps_the_default_and_never_loses_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LOG_RETENTION_DAYS", value)
    (tmp_path / "shopsim-2026-11-01.jsonl").write_text("{}\n")  # 3 jours : gardé (7 par défaut)
    write = handler(tmp_path)

    write.handle(record(MIDNIGHT, "first"))
    write.close()

    assert retention_days() == 7
    assert [p.name for _, p in segments(tmp_path, "shopsim")] == [
        "shopsim-2026-11-01.jsonl",
        "shopsim-2026-11-04.jsonl",
    ]
    assert json.loads(read_lines(tmp_path, "shopsim.jsonl")[-1])["event"] == "first"
