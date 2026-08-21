@echo off
setlocal
cd /d "%~dp0"
title ArchiveDB - Seed local dev data

echo ==============================================
echo ArchiveDB LOCAL DEV DATA SEED
echo ==============================================
echo This only resets data inside this repository's
echo local data folder. Production paths are refused.
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "BOOTPY=py -3"
) else (
    set "BOOTPY=python"
)

echo [1/2] Preparing Python environment...
%BOOTPY% scripts\dev_bootstrap.py --setup-only
if errorlevel 1 goto :error

set "SEEDPY=.venv\Scripts\python.exe"
if not exist "%SEEDPY%" (
    echo [error] Local virtual environment Python was not created.
    goto :error
)

echo.
echo [2/2] Seeding local library...
if "%~1"=="" (
    "%SEEDPY%" scripts\dev_seed_library.py
) else (
    "%SEEDPY%" scripts\dev_seed_library.py "%~1"
)
if errorlevel 1 goto :error

echo.
echo ==============================================
echo Seed complete. Run start.bat to open ArchiveDB.
echo ==============================================
pause
exit /b 0

:error
echo.
echo ==============================================
echo Seed FAILED. No production data was touched.
echo ==============================================
pause
exit /b 1
