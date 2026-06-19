FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app /app/app

RUN mkdir -p /data/exports

EXPOSE 8000

# Default command runs the API. The puller service overrides this in compose.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
