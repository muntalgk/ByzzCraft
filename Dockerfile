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
CMD ["python", "panel.py"]
