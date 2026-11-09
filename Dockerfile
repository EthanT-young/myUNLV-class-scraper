# Site + live seat API. Multi-arch base image, so it builds natively on ARM (Ampere).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SITE_DIR=/app/site \
    DATA_DIR=/data \
    PORT=8080

RUN pip install --no-cache-dir "requests==2.31.0" \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir /data && chown app /data

WORKDIR /app
COPY scraper/ scraper/
COPY server/ server/
COPY site/ site/

USER app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]
CMD ["python", "-m", "server"]
