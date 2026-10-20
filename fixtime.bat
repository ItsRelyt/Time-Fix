@echo off
title Fix Clock

if not exist "%~dp0fixtime.py" goto nofile

py -3 --version >nul 2>&1
if %errorlevel%==0 goto usepy

python --version >nul 2>&1
if %errorlevel%==0 goto usepython

goto nopython

:usepy
py -3 "%~dp0fixtime.py"
goto end

:usepython
python "%~dp0fixtime.py"
goto end

:nofile
echo Could not find fixtime.py. Keep it in the same folder as this file.
goto end

:nopython
echo Python was not found on this PC.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then run this file again.
goto end

:end
echo.
echo Press any key to close this window.
pause >nul
