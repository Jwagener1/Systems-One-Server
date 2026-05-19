# syntax=docker/dockerfile:1
# ── Stage 1: base — python + ODBC runtime ────────────────────────────────────
FROM python:3.12-slim AS base

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 unixodbc \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# ── Stage 2: builder ──────────────────────────────────────────────────────────
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        unixodbc-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt

# ── Stage 3: runtime ──────────────────────────────────────────────────────────
FROM base AS runtime

COPY --from=builder /install /usr/local

RUN groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid appuser --no-create-home appuser

WORKDIR /app
COPY app/ .

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8000/health',timeout=4); sys.exit(0 if r.status==200 else 1)"

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
