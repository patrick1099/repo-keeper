@echo off
REM Build repo-keeper-reanchor.exe (PyInstaller onefile).
REM Output: dist\repo-keeper-reanchor.exe  -- copy it next to your project's .clangd.
REM Keep the --name in step with toolname.REANCHOR_EXE; the placement code
REM looks the exe up by that name and will silently not find a mismatched one.
REM
REM Normally you do not need this file: a run that has to deploy the exe builds
REM it first (k2c_common.build_reanchor_exe, same flags as below). This stays
REM for the one thing that is not automatic -- installing PyInstaller.
REM
REM --specpath build: the .spec carries absolute paths including your home
REM directory, and build\ is gitignored and skipped by the leak sweep.
cd /d "%~dp0"
py -3 -m PyInstaller --version >nul 2>nul || py -3 -m pip install pyinstaller
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed -- check network/proxy and retry.
    exit /b 1
)
if not exist build mkdir build
py -3 -m PyInstaller --onefile --console -y --name repo-keeper-reanchor ^
    --distpath dist --workpath build --specpath build ReAnchor.py
echo.
echo Done: %~dp0dist\repo-keeper-reanchor.exe
