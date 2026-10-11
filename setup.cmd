@echo off
if "%~1"=="--clean" goto external
if exist "%~dp0.dfx-local\env\Scripts\python.exe" goto managed
:external
where py.exe >nul 2>nul
if not errorlevel 1 goto launcher
where python.exe >nul 2>nul
if errorlevel 1 (
  echo DFX setup needs Python 3.11+ on Windows x64. It creates its own locked project environment.
  exit /b 1
)
python.exe "%~dp0scripts\bootstrap.py" %*
exit /b %errorlevel%
:managed
"%~dp0.dfx-local\env\Scripts\python.exe" "%~dp0scripts\bootstrap.py" %*
exit /b %errorlevel%
:launcher
py.exe -3 "%~dp0scripts\bootstrap.py" %*
exit /b %errorlevel%
