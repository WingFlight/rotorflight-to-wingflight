@echo off
setlocal
cd /d %~dp0

if not defined CONVERTER_VERSION set CONVERTER_VERSION=0.1.0

echo [1/6] Checking for pyinstaller...
pyinstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller || goto :error
)

echo [2/6] Installing Python requirements...
pip install -r requirements_converter.txt || goto :error

echo [3/6] Generating version info (%CONVERTER_VERSION%)...
python gen_version_info.py || goto :error

echo [4/6] Compiling converter_gui.py to standalone EXE...
python -m PyInstaller --onefile --noupx converter_gui.py --name rotorflight-to-wingflight --windowed --uac-admin --version-file version_info.txt --icon icon.ico --add-data "drivers;drivers" --add-data "tools;tools" --add-data "logo.png;." || goto :error

echo [5/6] Moving rotorflight-to-wingflight.exe into parent folder...
if exist ..\rotorflight-to-wingflight.exe (
    del ..\rotorflight-to-wingflight.exe
)
move /Y dist\rotorflight-to-wingflight.exe ..\rotorflight-to-wingflight.exe >nul

echo [6/6] Cleaning up build tree...
rd /s /q build
rd /s /q dist
del /q rotorflight-to-wingflight.spec

echo Build complete. rotorflight-to-wingflight.exe is ready at: ..\rotorflight-to-wingflight.exe
goto :eof

:error
echo Build failed.
exit /b 1
