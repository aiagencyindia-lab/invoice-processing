# The API and the worker are the same image with different start commands.
# They share code, configuration and dependencies, and building them twice
# would only create a way for the two to drift apart.

FROM python:3.12-slim AS base

# uv resolves from the committed lock file, so a deployed image gets the
# versions the test suite ran against rather than whatever resolves today.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first: they change far less often than the source, so an edit
# to a handler does not reinstall the ADK and Vertex stack.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

COPY docker-entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

ENTRYPOINT ["/usr/local/bin/entrypoint"]

# Overridden for the worker. Railway supplies $PORT.
CMD ["sh", "-c", "uvicorn invoice_processing.api.service:app --host 0.0.0.0 --port ${PORT:-8000}"]
