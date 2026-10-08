@echo off
where pwsh.exe >nul 2>nul
if errorlevel 1 (
  echo DFX requires an existing PowerShell 7 installation with pwsh.exe available.
  exit /b 1
)
pwsh.exe -NoProfile -NonInteractive -File "%~dp0scripts\local.ps1" Prepare
if errorlevel 1 exit /b %errorlevel%
pwsh.exe -NoProfile -NonInteractive -File "%~dp0scripts\local.ps1" Init %*
exit /b %errorlevel%
