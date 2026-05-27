@echo off
REM ============================================================
REM  WebConsoleCapture - One-Click EXE Builder (Windows)
REM  Produces:  dist\WebConsoleCapture.exe
REM ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Building WebConsoleCapture.exe

echo.
echo ============================================================
echo   WebConsoleCapture - EXE Builder
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         IMPORTANT: tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2" %%V in ('python --version 2^>^&1') do set PYVER=%%V
echo [OK]  Found Python !PYVER!

if not exist .venv (
    echo [..] Creating virtual environment...
    python -m venv .venv || (echo [ERROR] venv creation failed & pause & exit /b 1)
)
call .venv\Scripts\activate.bat

echo [..] Installing dependencies (this may take a few minutes the first time)...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller || (echo [ERROR] pip install failed & pause & exit /b 1)

if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist WebConsoleCapture.spec del /q WebConsoleCapture.spec

echo [..] Building WebConsoleCapture.exe (one-file, no console)...
pyinstaller ^
    --noconfirm --onefile --windowed ^
    --name WebConsoleCapture ^
    --icon app\assets\icon.ico ^
    --add-data "app\assets;app\assets" ^
    --collect-submodules PySide6 ^
    --collect-submodules websocket ^
    --collect-submodules rapidocr_onnxruntime ^
    --hidden-import=PySide6.QtCore ^
    --hidden-import=PySide6.QtGui ^
    --hidden-import=PySide6.QtWidgets ^
    --hidden-import=websocket ^
    --hidden-import=mss ^
    --exclude-module tkinter ^
    --exclude-module PyQt5 ^
    --exclude-module PyQt6 ^
    app\__main__.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   SUCCESS - dist\WebConsoleCapture.exe
echo ============================================================
explorer "%cd%\dist"
pause
endlocal
