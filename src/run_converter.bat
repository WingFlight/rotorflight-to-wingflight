@echo off
REM Rotorflight to Wingflight Launcher

echo ========================================
echo Rotorflight to Wingflight
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.9 or higher from python.org
    echo.
    pause
    exit /b 1
)

echo Checking dependencies...
python -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install -r requirements_converter.txt
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo.
echo Starting converter...
echo Note: installing a DFU driver requires an Administrator command prompt.
echo.

pythonw converter_gui.py

if errorlevel 1 (
    echo.
    echo ERROR: Failed to start converter
    pause
    exit /b 1
)

exit /b 0

