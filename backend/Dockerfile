# LEGACYBRIDGE Platform application image: the React console built into static assets + the FastAPI app with
# its background engines (ONE process, ADR-F04). Build context: repository root.
FROM node:24-trixie-slim@sha256:4d00acb4bcc42c454c4eb801526f7ad7f772ea3bd4fd168c5cc2e384abcbb9d4 AS console
WORKDIR /console
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY frontend/ ./
COPY docs/api/openapi.json /docs/api/openapi.json
RUN npm run build

# The installer is needed to build the environment, not to run it: it stays in this stage.
FROM python:3.12-slim-trixie@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS environment
RUN pip install --no-cache-dir uv==0.13.9
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-trixie@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
# CVE-2026-124851 (2026-11-24): fixed in Debian's security archive, not yet in the published base; fails if it is not applied
RUN apt-get update -qq && apt-get install -y -qq --only-upgrade libzstd1 && rm -rf /var/lib/apt/lists/* \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libzstd1)" ge 1.5.7+dfsg-1+deb13u1
# nothing installs packages at run time
RUN pip uninstall -y -q --root-user-action=ignore pip && rm -rf /usr/local/lib/python3.12/ensurepip
RUN useradd --system --uid 10001 --home-dir /app lbf
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LBF_STATIC_DIR=/app/static
WORKDIR /app
COPY --from=environment /opt/venv /opt/venv
COPY --from=environment /app ./
COPY --from=console /console/dist ./static
# uid 10001 shares the SKARVIK exchange volume with the simulator
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=24 CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2); sys.exit(0)"
CMD ["python", "-m", "lbf.api.main"]
