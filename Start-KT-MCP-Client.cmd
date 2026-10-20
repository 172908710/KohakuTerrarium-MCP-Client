@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0runtime\python.exe" (
  echo Bundled Python is missing. Extract the COMPLETE portable ZIP.
  pause
  exit /b 1
)
"%~dp0runtime\python.exe" -I -X utf8 "%~dp0launch.py" %*
set "KT_EXIT=%ERRORLEVEL%"
if not "%KT_EXIT%"=="0" pause
exit /b %KT_EXIT%
