FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --home-dir /app --shell /usr/sbin/nologin appuser

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN pip install --no-cache-dir . && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD ["okx-nft-bot", "healthcheck"]

ENTRYPOINT ["okx-nft-bot"]
CMD ["run-daemon"]
