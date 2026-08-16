# syntax=docker/dockerfile:1.7

# Never `latest`. A floating tag on the image that resolves your dependency
# graph silently defeats `uv sync --frozen`: the lock is honoured by a resolver
# you did not choose.
#
# The Python tag is pinned to the minor line and the distro to trixie on
# purpose. Pinning a patch (`3.13.3-slim-bookworm`) froze the OS package set
# too, and the image accumulated eight HIGH/CRITICAL CVEs in openssl and
# perl-base that all had fixes available upstream — the scan caught exactly
# that. Renovate promotes this to a digest and bumps it, which is the only way
# a pin stays both reproducible and current.
ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.7
ARG APP_UID=10001

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-bin

FROM python:${PYTHON_VERSION}-slim-trixie AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app


FROM base AS builder

COPY --from=uv-bin /uv /uvx /bin/

# Dependencies resolve from the lock alone, in their own layer. Copying `src`
# first would rebuild the whole environment on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra redis --extra metrics --extra docs --extra outbound --extra mcp

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra redis --extra metrics --extra docs --extra outbound --extra mcp


FROM base AS runtime

ARG APP_UID
ARG APP_VERSION=dev
ARG APP_COMMIT=unknown
ARG APP_BUILT_AT=unknown

ENV APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT} \
    APP_BUILT_AT=${APP_BUILT_AT}

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${APP_UID} app \
    && useradd --uid ${APP_UID} --gid ${APP_UID} --shell /usr/sbin/nologin --no-create-home app

# Owned by root, readable by app: the runtime user has no business rewriting
# its own dependencies, and a writable venv turns any RCE into persistence.
COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --from=builder --chown=root:root /app/src /app/src
# The migrate stage runs `alembic upgrade head` from this image, so the config
# has to travel with it. Its paths are relative to WORKDIR, which is /app in
# both the build and the runtime stage.
COPY --chown=root:root alembic.ini /app/alembic.ini

# pip is not needed once the venv is built, and leaving it costs twice: its
# vendored copies of msgpack and setuptools ship known CVEs that a scanner will
# flag forever, and an attacker who gets code execution finds a package
# installer already in the container. Deleting the component is the honest fix;
# a .trivyignore would only hide the same bytes.
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip \
           /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3*

USER app
EXPOSE 8000

# tini reaps zombies and forwards SIGTERM. Without an init, PID 1 is Python,
# which ignores signals it has no handler for, so the orchestrator waits out
# the grace period and SIGKILLs mid-request on every deploy.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status == 200 else 1)"

CMD ["evenkeel-web"]
