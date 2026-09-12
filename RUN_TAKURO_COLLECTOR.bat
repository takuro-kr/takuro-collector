@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto run
call "%~dp0INSTALL_TAKURO_COLLECTOR.bat" --no-run
if errorlevel 1 exit /b 1
:run
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
exit /b 0
