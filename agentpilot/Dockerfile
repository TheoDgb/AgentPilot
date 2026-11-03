# Image d'AgentPilot (J7.1) : service FastAPI, serveurs MCP des agents (Support et Ops) et
# Claude Agent SDK, qui embarque la CLI Claude Code.
#
# Copies explicites seulement : jamais la vérité terrain (`evals/*.jsonl`), jamais le CLAUDE.md
# développeur, jamais `.env` (exclu du contexte par .dockerignore), jamais les tests. La clé
# d'API arrive par l'environnement du conteneur (docker-compose.yml), jamais dans l'image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

WORKDIR /srv
# Dépendances seulement (le code est copié à côté, importé depuis /srv)
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml --extra mcp --extra agent

COPY agentpilot ./agentpilot
COPY mcp_server ./mcp_server
COPY observability ./observability
# Consignes et guides des agents (pas de code)
COPY agents ./agents
# De evals/, les seuils de validation des actions et les énumérations des décisions, rien d'autre
COPY evals/__init__.py evals/policy.py evals/ground_truth.py ./evals/
RUN rm -rf agentpilot/tests mcp_server/tests observability/tests

# Utilisateur non root ; Compose le remplace par celui de l'hôte (fichiers de logs)
RUN useradd --uid 1000 --no-create-home --home-dir /tmp agentpilot
USER agentpilot
# La configuration de Claude Code est un dossier temporaire par exécution (CLAUDE_CONFIG_DIR) ;
# HOME seulement pour ce qui le demanderait quand même
ENV HOME=/tmp \
    PYTHONPATH=/srv \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8001
CMD ["uvicorn", "agentpilot.app:app", "--host", "0.0.0.0", "--port", "8001"]
