# Avian Visitors render server for the reTerminal E1002.
FROM python:3.12-slim-bookworm

ARG AV_REPO=https://github.com/Twarner491/AvianVisitors.git
ARG AV_REF=avian-visitors

RUN apt-get update && apt-get install -y --no-install-recommends git tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && git clone --depth 1 --branch "$AV_REF" "$AV_REPO" /opt/AvianVisitors \
 && cd /opt/AvianVisitors && find . -mindepth 1 -maxdepth 1 ! -name avian ! -name frame -exec rm -rf {} + \
 && pip install --no-cache-dir -r /opt/AvianVisitors/frame/requirements-shoot.txt numpy \
 && playwright install --with-deps chromium

COPY render_server.py /app/render_server.py
WORKDIR /app
ENV AV_FRAME_DIR=/opt/AvianVisitors/frame DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8088
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/status', timeout=5)"
LABEL org.opencontainers.image.title="avian-frame" \
      org.opencontainers.image.description="Avian Visitors collage renderer for the Seeed reTerminal E1002"
CMD ["python3", "/app/render_server.py"]
