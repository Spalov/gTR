@echo off
setlocal
set "gtr_runtime=%~dp0runtime"
if not exist "%gtr_runtime%\pythonw.exe" set "gtr_runtime=%~dp0.venv\Scripts"
set "PYTHONPATH=%~dp0"
if not exist "%gtr_runtime%\pythonw.exe" goto missing
if "%~1"=="" (
  start "" "%gtr_runtime%\pythonw.exe" -m gtr gui
  exit /b 0
)
"%gtr_runtime%\python.exe" -m gtr %*
exit /b %errorlevel%
:missing
chcp 65001 >nul
echo Сначала запустите setup.cmd или распакуйте полный комплект gTR.
pause
exit /b 1
