@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
  set "LAUNCHER="
) else (
  where py >nul 2>&1
  if not errorlevel 1 (
    set "PY="
    set "LAUNCHER=1"
  ) else (
    set "PY=python"
    set "LAUNCHER="
  )
)

if defined LAUNCHER (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python launcher "py" was not found. Install Python 3.12+ from python.org and try again.
    pause
    exit /b 1
  )
) else (
  if not exist "%PY%" (
    where "%PY%" >nul 2>&1
    if errorlevel 1 (
      echo Python was not found. Install Python 3.12+ from python.org and try again.
      pause
      exit /b 1
    )
  )
)

if defined LAUNCHER (
  py -3 -c "import debate" 2>nul
) else (
  "%PY%" -c "import debate" 2>nul
)
if errorlevel 1 (
  echo First run: installing Floor...
  if defined LAUNCHER (
    py -3 -m pip install -e .
  ) else (
    "%PY%" -m pip install -e .
  )
  if errorlevel 1 (
    echo Install failed.
    echo Details may also be in .run\launch.log after a later start.
    pause
    exit /b 1
  )
)

if defined LAUNCHER (
  py -3 -m debate launch --port 8765 --hours 3
) else (
  "%PY%" -m debate launch --port 8765 --hours 3
)
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo Floor failed to start. Read the error above, or open .run\launch.log
  pause
  exit /b %ERR%
)
