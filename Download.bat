@echo off
REM ---------------------------------------------------------------------
REM  Double-click launcher for the shop counter.
REM  On a fresh PC this installs everything it needs, then starts.
REM  You can also drag a YouTube link onto this file.
REM ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title ytshop

where python >nul 2>&1
if errorlevel 1 goto nopython

if not exist "ytshop.py" goto nofiles

REM Fast when everything is already installed; does the work when it isn't.
python setup.py
if errorlevel 1 goto setupfailed

REM Passing an empty argument would look like a blank link, so branch on it.
if "%~1"=="" (
    python ytshop.py
) else (
    python ytshop.py "%~1"
)

echo.
pause
exit /b 0

:nopython
echo.
echo   Python is not installed, or not on your PATH.
echo.
echo   1. Get it from  https://www.python.org/downloads/
echo   2. On the first screen, TICK "Add python.exe to PATH".
echo   3. Run this file again.
echo.
pause
exit /b 1

:nofiles
echo.
echo   ytshop.py is missing from this folder.
echo   Keep Download.bat, setup.py and ytshop.py together.
echo.
pause
exit /b 1

:setupfailed
echo.
echo   Setup did not finish. Look at the !! lines above.
echo.
pause
exit /b 1
