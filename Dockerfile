FROM python:3.13-slim

WORKDIR /app
# Headless renderer for website-post screenshots; see deaddit/websites/screenshot.py.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install from the uv lockfile (single source of truth; requirements.txt deleted)
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Container logs go to stdout only (collected by the Docker driver)
ENV DEADDIT_LOG_FILE=""

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY deaddit deaddit
COPY app.py gunicorn.conf.py ./
COPY migrations migrations
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 5000

# Production serving via gunicorn + tracked WSGI entrypoint (not the Flask dev server)
CMD ["gunicorn", "-c", "gunicorn.conf.py", "deaddit.wsgi:app"]
