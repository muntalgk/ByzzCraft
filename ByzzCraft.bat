@echo off
rem Double-click launcher. Runs from the folder this file is in, not from the desktop.
cd /d "%~dp0"
rem no ( ) blocks: the ) of (3, 10) would close them early
set "PY=py -3"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && goto run
rem The Microsoft Store "python" stub fails this check, so it is never picked by mistake.
set "PY=python"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && goto run
echo Python 3.10 or newer is needed.
echo Install it with: winget install -e --id Python.Python.3.12
echo or from https://www.python.org/downloads/ ticking "Add python.exe to PATH".
pause
exit /b 1
:run
%PY% panel.py %*
if errorlevel 1 pause