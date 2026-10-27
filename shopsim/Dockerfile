FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml ./
COPY shopsim ./shopsim
# Logs JSON et trace, communs aux trois services
COPY observability ./observability
RUN uv pip install --system --no-cache .

EXPOSE 8000
CMD ["uvicorn", "shopsim.app.main:app", "--host", "0.0.0.0", "--port", "8000"]