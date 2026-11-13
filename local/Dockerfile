# Imagen solo para desarrollo local (docker compose -f local/docker-compose.yml up).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements/ requirements/
RUN pip install --no-cache-dir -r requirements/dev.txt
