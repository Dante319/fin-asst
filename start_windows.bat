@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==================================================
echo   fin-asst
echo ==================================================
echo.
echo Setting things up. The first run installs a couple of small tools and
echo can take a minute or two; after that it starts in a few seconds.
echo.
echo This window has to stay open while the app is running -- closing it
echo stops the app. Your data is not affected either way.
echo.

set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"

where uv >nul 2>nul
if errorlevel 1 (
    echo Installing uv ^(the tool this app uses to manage itself^)...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo Could not install uv automatically. Ask whoever shared this app with
    echo you for help, or see: https://docs.astral.sh/uv/getting-started/installation/
    echo.
    pause
    exit /b 1
)

set "UV_PROJECT_ENVIRONMENT=%USERPROFILE%\.venvs\fin-asst"

echo Installing the app...
uv sync
if errorlevel 1 goto :error

echo Preparing your personal database (this stays on your computer only)...
uv run finasst init >nul
if errorlevel 1 goto :error

echo.
echo Starting fin-asst -- your browser will open automatically in a moment.
echo To stop the app later, come back to this window and press Ctrl-C,
echo or just close the window.
echo.

start "" powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'"

uv run finasst serve
goto :end

:error
echo.
echo Something went wrong during setup. Ask whoever shared this app with you
echo for help, and share the message shown above.
echo.

:end
pause
