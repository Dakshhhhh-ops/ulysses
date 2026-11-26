# The agent side of the Ulysses split: the scripted agent and the Ulysses MCP server, nothing else.
#
# The image holds code only. It is built from an explicit list of paths (no `COPY . .`), so the
# project's .env, the .run folder (service database, judge keys, the demo shop's server-side key)
# and any signer key can never be baked in; .dockerignore keeps them out of the build context too.
# At run time the container gets two settings, ULYSSES_URL and ULYSSES_AGENT_TOKEN, and no volume.
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && rm -rf ./src
COPY scripts/agent_session.py scripts/agent_boundary_check.py ./scripts/

RUN useradd --uid 10001 --no-create-home --home-dir /nonexistent agent
USER 10001:10001
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/nonexistent
ENTRYPOINT ["python", "scripts/agent_session.py"]
