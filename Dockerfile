# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies are baked into the image at build time.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user.
RUN useradd --system --uid 10001 appuser
USER appuser

EXPOSE 8000

# Startup is fully self-contained: no external services or init steps needed.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
