# ByzzCraft in Docker: the panel and the Minecraft server in one container. Java is downloaded by the panel
# into /data (the right version for the server jar), so the image stays small.
#   docker compose up -d   then   docker compose logs   for the setup link
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 byzz && mkdir /data /minecraft && chown byzz:byzz /data /minecraft
WORKDIR /app
COPY --chown=byzz:byzz . .
USER byzz

# data/ of the panel (users, settings, logs, Java) and the Minecraft server folder: two volumes
ENV BYZZ_DATA=/data BYZZ_SERVER_DIR=/minecraft PYTHONUNBUFFERED=1
VOLUME ["/data", "/minecraft"]
EXPOSE 8765 25565
# GET / answers 200 (panel, login or setup page) as soon as the panel is up; the port can be changed in the config
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import json, os, urllib.request as u; c = '/data/panel-config.json'; p = (json.load(open(c)).get('port') if os.path.exists(c) else 0) or 8765; u.urlopen('http://127.0.0.1:%d/' % p, timeout=4)"]
CMD ["python", "panel.py"]
