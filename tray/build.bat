@echo off
rem Builds "dist\ATK Battery.exe" (single file, no console window).
rem Needs Python 3.10 - 3.14 (hidapi has no Windows build for newer versions yet).
setlocal
cd /d "%~dp0"

set PY=
for %%V in (3.13 3.12 3.14 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set PY=py -%%V
  )
)
if not defined PY (
  echo No supported Python found. Install one with:
  echo     py install 3.13
  echo then run this again.
  goto :fail
)
echo Using %PY%

rem Rebuild the venv if it was made with a different Python
if exist .venv\Scripts\python.exe (
  for /f "delims=" %%A in ('%PY% -c "import sys;print(sys.version_info[:2])"') do set WANT=%%A
  for /f "delims=" %%B in ('.venv\Scripts\python.exe -c "import sys;print(sys.version_info[:2])"') do set HAVE=%%B
)
if defined HAVE if not "%WANT%"=="%HAVE%" (
  echo Removing old build environment made with a different Python...
  rmdir /s /q .venv
)

if not exist .venv (
  %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat || goto :fail

python -m pip install --upgrade pip >nul
python -m pip install --only-binary hidapi -r requirements.txt pyinstaller || goto :fail

pyinstaller --noconfirm --onefile --windowed ^
  --name "ATK Battery" ^
  --icon app.ico ^
  --hidden-import pystray._win32 ^
  atk_tray.pyw || goto :fail

echo.
echo Built: %CD%\dist\ATK Battery.exe
echo Copy it somewhere permanent (e.g. %%LOCALAPPDATA%%\ATKBattery) before
echo ticking "Start with Windows" in its menu.
pause
exit /b 0

:fail
echo.
echo Build failed - see the messages above.
pause
exit /b 1
