@echo off
setlocal
cd /d "%~dp0"

py -3 -m PyInstaller --version >nul 2>nul || py -3 -m pip install pyinstaller
if errorlevel 1 (
    echo ERROR: PyInstaller is unavailable -- check Python, network, and proxy settings.
    exit /b 1
)

if not exist build mkdir build
py -3 -m PyInstaller --onefile --console --clean -y --name repo-hygiene ^
    --distpath dist --workpath build --specpath build RepoHygiene.py
if errorlevel 1 exit /b 1

set "BIN_DIR=%LOCALAPPDATA%\Programs\bin"
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"
if errorlevel 1 exit /b 1

copy /y "dist\repo-hygiene.exe" "%BIN_DIR%\repo-hygiene.exe" >nul
if errorlevel 1 exit /b 1

echo Installed: %BIN_DIR%\repo-hygiene.exe
echo Run: repo-hygiene -p ^<repo^>
endlocal
