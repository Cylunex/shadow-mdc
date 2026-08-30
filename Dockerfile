FROM node:22-alpine AS web
WORKDIR /build/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM python:3.12-slim
ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src/ src/
COPY --from=web /build/web/dist/ src/shadow_mdc/static/
RUN pip install --no-cache-dir .
VOLUME ["/app/data", "/media"]
EXPOSE 8000
CMD ["uvicorn", "shadow_mdc.api:app", "--host", "0.0.0.0", "--port", "8000"]
