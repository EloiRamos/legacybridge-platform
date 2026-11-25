# Migration runner image (one-shot `migrate` service). Runs Alembic as lbf_migrator.
FROM python:3.12-slim-trixie@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS environment
RUN pip install --no-cache-dir uv==0.13.9
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-trixie@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
# CVE-2026-124851 (2026-11-24): fixed in Debian's security archive, not yet in the published base; fails if it is not applied
RUN apt-get update -qq && apt-get install -y -qq --only-upgrade libzstd1 && rm -rf /var/lib/apt/lists/* \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libzstd1)" ge 1.5.7+dfsg-1+deb13u1
# nothing installs packages at run time
RUN pip uninstall -y -q --root-user-action=ignore pip && rm -rf /usr/local/lib/python3.12/ensurepip
RUN useradd --system --home-dir /app lbf
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=environment /opt/venv /opt/venv
COPY --from=environment /app ./
USER lbf
CMD ["alembic", "upgrade", "head"]
