FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml ./
COPY shopsim ./shopsim
# Logs JSON et trace, communs aux trois services
COPY observability ./observability
# Générateur de trafic (service `traffic` du profil du même nom) : bibliothèque standard
COPY traffic ./traffic
# Données de démo au premier démarrage (`shopsim/seed.py`) : de evals/, les seuils de la politique
# et le schéma des tickets, jamais la vérité terrain (`evals/*.jsonl`)
COPY evals/__init__.py evals/policy.py evals/ground_truth.py ./evals/
RUN uv pip install --system --no-cache .

EXPOSE 8000
CMD ["uvicorn", "shopsim.app.main:app", "--host", "0.0.0.0", "--port", "8000"]