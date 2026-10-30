# Image for Azure Container Apps (or any container host). The app listens on $PORT.
FROM python:3.12-slim

# ICU is required by the Nutrient Python SDK native runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libicu-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend backend
COPY frontend frontend
COPY inputs inputs

EXPOSE 8080
WORKDIR /app/backend
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
