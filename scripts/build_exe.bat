@echo off
REM Build repo-keeper-reanchor.exe (PyInstaller onefile).
REM Output: dist\repo-keeper-reanchor.exe  -- copy it next to your project's .clangd.
REM Keep the --name in step with toolname.REANCHOR_EXE; the placement code
REM looks the exe up by that name and will silently not find a mismatched one.
cd /d "%~dp0"
py -3 -m PyInstaller --version >nul 2>nul || py -3 -m pip install pyinstaller
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed -- check network/proxy and retry.
    exit /b 1
)
py -3 -m PyInstaller --onefile --console --name repo-keeper-reanchor ReAnchor.py
echo.
echo Done: %~dp0dist\repo-keeper-reanchor.exe
