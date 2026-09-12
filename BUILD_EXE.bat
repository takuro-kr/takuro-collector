@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  call "%~dp0INSTALL_TAKURO_COLLECTOR.bat" --no-run
  if errorlevel 1 exit /b 1
)
echo Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6,<7"
if errorlevel 1 goto failed
echo Building Windows EXE...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --name "TAKURO Updater" updater_helper_main.py
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean TAKURO_Collector.spec
if errorlevel 1 goto failed
copy /y ".\dist\TAKURO Updater.exe" ".\dist\TAKURO Collector\TAKURO Updater.exe" >nul
if errorlevel 1 goto failed
".venv\Scripts\python.exe" scripts\write_update_package_metadata.py ".\dist\TAKURO Collector\update-package.json"
if errorlevel 1 goto failed
if exist "TAKURO-Collector-Portable-Windows.zip" del /q "TAKURO-Collector-Portable-Windows.zip"
powershell.exe -NoProfile -Command "Compress-Archive -Path '.\dist\TAKURO Collector\*' -DestinationPath '.\TAKURO-Collector-Portable-Windows.zip' -Force"
if errorlevel 1 goto failed
echo.
echo Build complete:
echo   dist\TAKURO Collector\TAKURO Collector.exe
echo   TAKURO-Collector-Portable-Windows.zip
explorer.exe "%~dp0dist\TAKURO Collector"
pause
exit /b 0
:failed
echo.
echo EXE build failed. Run DIAGNOSE.bat and check the error above.
pause
exit /b 1
