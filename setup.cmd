@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.14 -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info[:3] != (3, 14, 10) or sys.maxsize < 2**32)"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements-win64.txt
if errorlevel 1 goto failed
echo Настройка завершена. Откройте gTR.cmd.
pause
exit /b 0
:failed
echo Проверьте наличие CPython 3.14.10 x64 и доступ к PyPI.
pause
exit /b 1
