@echo off
REM Builds the app folder dist\UniversalVideoStudio (exe + yt-dlp). FFmpeg and Deno are downloaded by the app on first launch.
setlocal
cd /d "%~dp0"

REM CI (GitHub Actions) sets CI=true: use the runner's Python and never wait for a key press.
set "PAUSE=pause"
if defined CI set "PAUSE=echo."
if defined CI (
  set "VPY=python"
) else (
  if not exist ".venv\Scripts\python.exe" (
    echo Run run.bat once first to create the virtual environment.
    %PAUSE% & exit /b 1
  )
  set "VPY=.venv\Scripts\python.exe"
)
%VPY% -m pip install -r requirements-dev.txt || (echo pip failed & %PAUSE% & exit /b 1)
REM Judge the tests by pytest's own summary line (some Windows setups return a non-zero exit code
REM from the Qt shutdown even when every test passed).
set "TESTLOG=%TEMP%\uvs_tests.log"
%VPY% -m pytest -q > "%TESTLOG%" 2>&1
type "%TESTLOG%"
findstr /R /C:"[0-9][0-9]* failed" /C:"[0-9][0-9]* error" "%TESTLOG%" >nul && (echo Tests failed - build aborted. & %PAUSE% & exit /b 1)
findstr /R /C:"[0-9][0-9]* passed" "%TESTLOG%" >nul || (echo Tests did not finish - build aborted. & %PAUSE% & exit /b 1)

REM The .exe file icon: put your own icon.ico in the assets folder (it is used automatically).
set "ICONARG="
if exist "assets\icon.ico" set "ICONARG=--icon assets\icon.ico"

REM Instant splash: shown by the exe bootloader before Python even starts (needs tkinter, part of a normal Python install).
set "SPLASHARG="
if exist "assets\splash.png" %VPY% -c "import tkinter" >nul 2>nul && set "SPLASHARG=--splash assets\splash.png"

%VPY% -m PyInstaller --noconfirm --clean --windowed --name UniversalVideoStudio %ICONARG% %SPLASHARG% ^
  --collect-submodules yt_dlp --collect-data yt_dlp --copy-metadata yt-dlp --collect-all curl_cffi --collect-all certifi ^
  --collect-submodules PySide6.QtMultimedia ^
  --hidden-import PySide6.QtWebEngineWidgets --hidden-import PySide6.QtWebEngineCore ^
  main.py || (echo PyInstaller failed & %PAUSE% & exit /b 1)

REM FFmpeg and Deno are NOT bundled: the app downloads them (SHA-256 verified) on first launch into %%APPDATA%%.
echo yt-dlp is bundled; FFmpeg and Deno are downloaded by the app on first launch.

REM SECURITY: the build must never contain sign-in cookies or personal data.
REM Settings, cookies and history live in %%APPDATA%%\UniversalVideoStudio on the user's PC, not here.
if exist "dist\UniversalVideoStudio\data" rmdir /S /Q "dist\UniversalVideoStudio\data"
if exist "dist\UniversalVideoStudio\cookies" rmdir /S /Q "dist\UniversalVideoStudio\cookies"
echo.
echo Build ready: dist\UniversalVideoStudio\UniversalVideoStudio.exe  (no cookies, no FFmpeg/Deno)
echo Videos go to: %%USERPROFILE%%\Videos\UniversalVideoStudio\Video  and  \Cache video
endlocal
%PAUSE%
