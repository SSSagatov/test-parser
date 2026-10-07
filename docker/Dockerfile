FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    UV_LINK_MODE=copy
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libreoffice-writer tesseract-ocr tesseract-ocr-rus \
    tesseract-ocr-kaz tesseract-ocr-eng unar \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.10
COPY pyproject.toml uv.lock README.md alembic.ini main.py ./
COPY app ./app
COPY migrations ./migrations
RUN uv sync --frozen --no-dev && .venv/bin/python -m playwright install --with-deps chromium
COPY docker/entrypoint.py ./docker/entrypoint.py
RUN useradd --create-home --uid 10001 crawler \
    && mkdir -p /app/data/downloads /app/data/exports /app/logs \
    && chown -R crawler:crawler /app/data /app/logs /opt/playwright
USER crawler
ENTRYPOINT ["/app/.venv/bin/python", "/app/docker/entrypoint.py"]
CMD ["crawl", "--export", "excel"]
