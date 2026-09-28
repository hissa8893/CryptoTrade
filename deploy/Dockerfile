# UNTESTED (no Docker daemon was available where this was written). Linux hosts only.
# The app refuses to listen on 0.0.0.0 (safety rule), so run the container on the host network:
#   docker build -f deploy/Dockerfile -t cryptotrade .
#   docker run -d --name cryptotrade --network host -v "$PWD/data:/app/data" -v "$PWD/logs:/app/logs" \
#              -v "$PWD/config.yaml:/app/config.yaml:ro" -v "$PWD/.env:/app/.env:ro" cryptotrade
# then open http://127.0.0.1:8765 on the host.
# Secrets never enter the image: .dockerignore excludes .env, config.yaml and data/, and the
# local tokens are generated when the container first starts (into the mounted folders).
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TRADER_HOME=/app
COPY requirements.txt requirements-llm.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-llm.txt
COPY . .
RUN pip install --no-cache-dir --no-deps -e . && useradd --create-home trader && chown -R trader /app
USER trader
CMD ["sh", "-c", "python -m trader init && python -m trader db migrate && exec python -m trader serve"]
