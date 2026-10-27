"""Pas d'agent orphelin : la CLI et le serveur MCP s'arrêtent avec l'exécution ou le service.

Une fausse CLI (script Python) remplace Claude Code, sans appel à l'API : elle passe par le
vrai lanceur (`claude_guard`) et le vrai SDK, lance le vrai serveur MCP décrit par
`--mcp-config`, note les deux PID, puis ne répond jamais. Linux seulement (`/proc`,
`PR_SET_PDEATHSIG`).
"""

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from agentpilot import sdk_runner
from agentpilot.claude_guard import CLI_ENV
from agentpilot.hooks import SupportHooks
from agentpilot.models import AgentKind, Run, RunStatus, RunTrigger
from agentpilot.schemas import SupportDecision
from agentpilot.support_agent import REPO_ROOT
from agentpilot.tests.conftest import TODAY, MemoryJournal
from agentpilot.worker import Worker, new_run

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux seulement")

FAKE_CLI = """\
#!{python}
# Fausse CLI Claude Code : lance le serveur MCP configuré, note les PID, ne répond jamais
import json, os, signal, subprocess, sys, time

if "-v" in sys.argv:
    print("2.1.310 (Claude Code)")
    sys.exit(0)
config = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])
[server] = config["mcpServers"].values()
child = subprocess.Popen(
    [server["command"], *server["args"]],
    env={{**os.environ, **server["env"]}},
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
)
with open(os.environ["FAKE_CLI_PIDS"] + ".tmp", "w") as pids:
    pids.write(f"{{os.getpid()}} {{child.pid}}")
os.replace(os.environ["FAKE_CLI_PIDS"] + ".tmp", os.environ["FAKE_CLI_PIDS"])
if os.environ.get("FAKE_CLI_STUBBORN") == "1":
    # Ni SIGTERM ni fin de stdin ne l'arrêtent : seul SIGKILL le peut
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(1)
# Comme la vraie CLI : s'arrête quand le SDK ferme son stdin
sys.stdin.read()
if os.environ.get("FAKE_CLI_IGNORE_EOF") == "1":
    # CLI qui tarde à finir : il faut l'escalade du SDK (SIGTERM)
    while True:
        time.sleep(1)
"""


def alive(pid: int) -> bool:
    """Processus présent et pas zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def gone(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def read_pids(path: Path, timeout_s: float = 15.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        assert time.monotonic() < deadline, "la fausse CLI n'a pas démarré"
        time.sleep(0.05)
    cli, server = path.read_text().split()
    return int(cli), int(server)


async def read_pids_async(path: Path) -> tuple[int, int]:
    return await asyncio.to_thread(read_pids, path)


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Fausse CLI ; le fichier des PID est `tmp_path / "pids"`. Nettoyage en cas d'échec."""
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI.format(python=sys.executable))
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_CLI_PIDS", str(tmp_path / "pids"))
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    yield cli
    if (tmp_path / "pids").exists():
        for pid in read_pids(tmp_path / "pids"):
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


def environ(cli: Path) -> dict[str, str]:
    return {"ANTHROPIC_API_KEY": "sk-ant-test", CLI_ENV: str(cli)}


async def launch(cli: Path, timeout_s: float) -> sdk_runner.AgentResult:
    return await sdk_runner.run_agent(
        "Traite le ticket n°1.",
        SupportDecision,
        TODAY,
        hooks=SupportHooks(1, MemoryJournal()),
        environ=environ(cli),
        timeout_s=timeout_s,
    )


@pytest.mark.anyio
async def test_timeout_stops_the_cli_and_the_mcp_server(fake_cli: Path, tmp_path: Path) -> None:
    result = await launch(fake_cli, timeout_s=3)

    assert result.error is not None and "n'a pas répondu" in result.error
    cli, server = read_pids(tmp_path / "pids")
    assert gone(cli), "CLI encore vivante après le délai dépassé"
    assert gone(server), "serveur MCP encore vivant après le délai dépassé"


@pytest.mark.anyio
async def test_timeout_stops_a_cli_that_ignores_end_of_input(
    fake_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le SDK attend 5 s après la fermeture de stdin, puis envoie SIGTERM : cette escalade
    n'a lieu que si l'annulation vient d'anyio (`fail_after`), pas d'`asyncio.timeout`."""
    monkeypatch.setenv("FAKE_CLI_IGNORE_EOF", "1")

    await launch(fake_cli, timeout_s=3)

    cli, server = read_pids(tmp_path / "pids")
    assert gone(cli, timeout_s=15), "CLI encore vivante : l'escalade du SDK n'a pas eu lieu"
    assert gone(server)


@pytest.mark.anyio
async def test_service_shutdown_stops_running_agents(
    fake_cli: Path, tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    """Comme un Ctrl+C : le lifespan appelle `Worker.shutdown`."""
    worker = Worker(SimpleNamespace(session_factory=session_factory))
    run = new_run(session_factory, AgentKind.SUPPORT, 1, RunTrigger.LOOP, check_budget=False)
    task = asyncio.create_task(
        worker.execute(run.id, 1, RunTrigger.LOOP, lambda: launch(fake_cli, timeout_s=120))
    )
    cli, server = await read_pids_async(tmp_path / "pids")

    await worker.shutdown(timeout_s=20)
    await task

    assert gone(cli), "CLI encore vivante après l'arrêt du service"
    assert gone(server), "serveur MCP encore vivant après l'arrêt du service"
    with session_factory() as session:
        assert session.get_one(Run, run.id).status is RunStatus.INTERRUPTED


HARNESS = """\
import asyncio
from agentpilot.tests.test_process_cleanup import launch
from pathlib import Path
asyncio.run(launch(Path({cli!r}), timeout_s=600))
"""


def test_killed_service_leaves_no_orphan(fake_cli: Path, tmp_path: Path) -> None:
    """SIGKILL du service : ni `atexit` ni `finally`. Seul le noyau arrête la CLI."""
    service = subprocess.Popen(
        [sys.executable, "-c", HARNESS.format(cli=str(fake_cli))],
        cwd=REPO_ROOT,
        env=os.environ | {"FAKE_CLI_STUBBORN": "1"},
    )
    try:
        cli, server = read_pids(tmp_path / "pids")
        assert alive(cli) and alive(server)

        service.kill()
        service.wait(timeout=5)

        assert gone(cli), "CLI orpheline après la mort brutale du service"
        assert gone(server), "serveur MCP orphelin après la mort brutale du service"
    finally:
        service.kill()


INTERMEDIATE = """\
import subprocess, sys, time
server = subprocess.Popen(
    [sys.executable, "-m", "mcp_server.server"], stdin=int(sys.argv[1]),
    stdout=subprocess.DEVNULL, pass_fds=(int(sys.argv[1]),),
)
print(server.pid, flush=True)
time.sleep(600)
"""


def test_mcp_server_exits_when_its_parent_dies() -> None:
    """Le parent (la CLI) meurt mais stdin reste ouvert : seul le serveur peut s'en apercevoir."""
    read_end, write_end = os.pipe()
    parent = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(INTERMEDIATE), str(read_end)],
        cwd=REPO_ROOT,
        pass_fds=(read_end,),
        stdout=subprocess.PIPE,
        text=True,
    )
    os.close(read_end)
    try:
        assert parent.stdout is not None
        server = int(parent.stdout.readline())
        # Le serveur a lu le PID de son parent avant de le perdre
        time.sleep(1)

        parent.kill()
        parent.wait(timeout=5)

        assert gone(server), "serveur MCP orphelin alors que la CLI est morte"
    finally:
        os.close(write_end)
        parent.kill()
