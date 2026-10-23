#!/bin/sh
# ByzzCraft example start script for Linux: copy it into the Minecraft server folder.
# The panel needs two things from it: a world backup before each start (backups/world_*.zip)
# and the GC log in logs/gc.log (heap chart). Needs `zip` (apt install zip).
cd "$(dirname "$0")" || exit 1

RAM=${RAM:-4G}
JAVA=${JAVA:-java}

# world backup before every start (the panel cleans up the old ones)
if [ -d world ]; then
  mkdir -p backups
  TS=$(date +%Y-%m-%d_%H-%M-%S)
  echo "Backing up the world..."
  # write to .part and rename only on success: a half-written zip must not look like a good backup
  if zip -qr "backups/world_$TS.zip.part" world -x 'world/session.lock'; then
    mv "backups/world_$TS.zip.part" "backups/world_$TS.zip"
  else
    echo "WARNING: backup failed."
    rm -f "backups/world_$TS.zip.part"
  fi
fi

mkdir -p logs
exec "$JAVA" -Xms$RAM -Xmx$RAM \
  -XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 \
  -XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:+AlwaysPreTouch \
  -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M \
  -XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 \
  -XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90 \
  -XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem \
  -XX:MaxTenuringThreshold=1 -Dusing.aikars.flags=https://mcflags.emc.gs -Daikars.new.flags=true \
  -Xlog:gc:file=logs/gc.log:time,uptime:filecount=3,filesize=10M \
  -jar server.jar nogui
