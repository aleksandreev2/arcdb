@echo off
setlocal
cd /d "%~dp0"

echo === Updating ArchiveDB ===
where git >nul 2>nul
if not %errorlevel%==0 (
    echo Git was not found. Install Git for Windows first.
    pause
    exit /b 1
)

git pull --ff-only
if not %errorlevel%==0 (
    echo.
    echo Git pull failed. Local changes may need attention.
    pause
    exit /b 1
)

call start.bat
endlocal
