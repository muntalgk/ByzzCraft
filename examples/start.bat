@echo off
rem ByzzCraft example start script for Windows: copy it into the Minecraft server folder.
rem The panel needs two things from it: a world backup before each start (backups\world_*.zip)
rem and the GC log in logs\gc.log (heap chart).
title Minecraft server
cd /d "%~dp0"

rem RAM for the server (Xms = Xmx, as in Aikar's flags)
set RAM=4G
rem Java: set JAVA to a full path if java is not in PATH
if not defined JAVA set "JAVA=java"
rem the panel passes PYTHON too: the backup uses its zipfile module (Git's tar in PATH cannot write zips)
if not defined PYTHON set "PYTHON=python"
rem jar to run: the panel passes the one it found (Fabric, Paper or the vanilla server.jar)
if not defined SERVER_JAR set "SERVER_JAR=server.jar"

rem --- world backup before every start (the panel cleans up the old ones)
if not exist "world" goto start
if not exist "backups" mkdir "backups"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm-ss"') do set TS=%%i
echo Backing up the world...
rem write to .part and rename only on success: a half-written zip must not look like a good backup
"%PYTHON%" -m zipfile -c "backups\world_%TS%.zip.part" world
if errorlevel 1 (
  echo WARNING: backup failed.
  del "backups\world_%TS%.zip.part" 2>nul
) else (
  ren "backups\world_%TS%.zip.part" "world_%TS%.zip"
)

:start
if not exist "logs" mkdir "logs"
"%JAVA%" -Xms%RAM% -Xmx%RAM% ^
 -XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 ^
 -XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:+AlwaysPreTouch ^
 -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M ^
 -XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 ^
 -XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90 ^
 -XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem ^
 -XX:MaxTenuringThreshold=1 -Dusing.aikars.flags=https://mcflags.emc.gs -Daikars.new.flags=true ^
 --enable-native-access=ALL-UNNAMED ^
 -Xlog:gc:file=logs/gc.log:time,uptime:filecount=3,filesize=10M ^
 -jar "%SERVER_JAR%" nogui

echo.
echo Server stopped.
rem the panel sets NOPAUSE so the window does not wait for a key
if not defined NOPAUSE pause
