@echo off
REM ============================================================
REM  DialogFilter - One-click EXE builder
REM  Produces:  dist\DialogFilter.exe
REM ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Building DialogFilter.exe

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    pause & exit /b 1
)

if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install pyinstaller >nul

if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist DialogFilter.spec del /q DialogFilter.spec

pyinstaller --noconfirm --onefile --windowed ^
    --name DialogFilter ^
    dialog_filter_app.py

if errorlevel 1 (
    echo [ERROR] Build failed.
    pause & exit /b 1
)

echo.
echo ============================================================
echo   SUCCESS - dist\DialogFilter.exe
echo ============================================================
explorer "%cd%\dist"
pause
endlocal
