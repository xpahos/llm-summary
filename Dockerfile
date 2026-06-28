FROM python:3.12-alpine

# System packages needed to build native wheels (cffi/cryptography etc.)
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    sqlite

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Application code, templates, assets and entrypoint.
COPY templates ./templates
COPY assets ./assets
COPY config ./config
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Make the source importable and locate templates/assets relative to /app.
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV LLM_SUMMARY_BASE_DIR=/app

# Sensible defaults; secrets must come from the environment at runtime.
ENV LLM_SUMMARY_CONFIG=/config/config.toml
ENV LLM_SUMMARY_DB=/data/llm-summary.sqlite
ENV LLM_SUMMARY_SITE=/site

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["run-daily"]
