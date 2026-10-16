# Treasury engine image. Build context = this directory.  docker build -t treasury-engine:<tag> .
# Pin the base by digest in your registry pipeline (FROM python:3.12-slim@sha256:...).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8200 METRICS_PORT=9100
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY ledger.py rails.py banks.py engine.py server.py store.py /app/
USER 10001
EXPOSE 8200 9100
CMD ["python3", "-I", "/app/server.py"]
