@echo off
setlocal
cd /d "%~dp0"

echo === ArchiveDB local launcher ===
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 scripts\dev_bootstrap.py
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python scripts\dev_bootstrap.py
    goto :done
)

echo.
echo Python 3 was not found.
echo Install Python 3.11+ from https://www.python.org/downloads/ and enable "Add Python to PATH".
exit /b 1

:done
if not %errorlevel%==0 (
    echo.
    echo ArchiveDB stopped with an error.
    pause
)
endlocal
