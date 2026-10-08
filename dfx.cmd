@echo off
if not exist "%~dp0.dfx-local\env\Scripts\dfx.exe" (
  echo DFX command is not installed. Run setup.cmd from this project directory.
  exit /b 1
)
"%~dp0.dfx-local\env\Scripts\dfx.exe" --workspace "%~dp0." %*
exit /b %errorlevel%
