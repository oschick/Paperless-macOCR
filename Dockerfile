# ---------- build stage ----------
FROM python:3.13-slim AS builder

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# Dependencies come from uv.lock (hash-checked); uv only exports it in this build stage.
COPY --from=ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f /uv /uvx /bin/
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
    pip install --no-cache-dir --require-hashes --prefix=/install -r /tmp/requirements.txt && \
    pip install --no-cache-dir --no-deps --prefix=/install .

# ---------- runtime stage ----------
FROM python:3.13-slim

LABEL org.opencontainers.image.title="paperless-macocr"
LABEL org.opencontainers.image.description="Paperless-NGX webhook service that re-OCRs documents via macOCR"
LABEL org.opencontainers.image.source="https://github.com/OWNER/paperlessMACOCR"

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/sh appuser

COPY --from=builder /install /usr/local

WORKDIR /app
USER appuser

ENV HOST=0.0.0.0
ENV PORT=9000

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:9000/health').raise_for_status()"

ENTRYPOINT ["paperless-macocr"]
