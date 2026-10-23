@echo off
REM Universal Video Studio launcher. Creates a local virtual environment on first run (nothing is installed globally).
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && (py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11")
if not defined PY (py -3 -c "import sys" >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python 3.11 was not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [first run] Creating virtual environment in .venv ...
  %PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
  echo [first run] Installing dependencies ^(downloads about 250 MB, needs internet^) ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Dependency installation failed. & pause & exit /b 1)
)

REM Re-install dependencies automatically when requirements.txt changed (e.g. after an update of this app).
".venv\Scripts\python.exe" tools\ensure_deps.py || (echo Dependency update failed. & pause & exit /b 1)

".venv\Scripts\python.exe" main.py %*
if errorlevel 1 (
  echo.
  echo The application exited with an error. Run "run.bat --check" for diagnostics or see logs in %%APPDATA%%\UniversalVideoStudio\logs
  pause
)
endlocal
