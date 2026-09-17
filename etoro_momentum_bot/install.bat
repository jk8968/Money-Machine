@echo off
setlocal EnableExtensions
title 3/6/12 Momentum Bot - Installer

echo ============================================================
echo        3/6/12 MOMENTUM BOT - INSTALLER
echo ============================================================
echo.

REM ------------------------------------------------------------
REM Find an existing Python installation
REM ------------------------------------------------------------

set "PYTHON="

where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=py -3"
    goto :python_found
)

where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python"
    goto :python_found
)

REM ------------------------------------------------------------
REM Python not found - install using Windows Package Manager
REM ------------------------------------------------------------

echo [1/4] Python was not found.
echo Installing Python...
echo.

where winget >nul 2>&1
if not %errorlevel%==0 (
    echo ERROR: Windows Package Manager ^(winget^) was not found.
    echo.
    echo Please install "App Installer" from the Microsoft Store
    echo and then run this installer again.
    echo.
    pause
    exit /b 1
)

winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements

if not %errorlevel%==0 (
    echo.
    echo ERROR: Python installation failed.
    pause
    exit /b 1
)

echo.
echo Python installation completed.
echo Searching for the new Python installation...
echo.

REM Try py launcher again
where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=py -3"
    goto :python_found
)

REM Try normal python command
where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python"
    goto :python_found
)

REM Search common Python installation directories
for /f "delims=" %%P in ('powershell -NoProfile -Command "$p = Get-ChildItem \"$env:LOCALAPPDATA\Programs\Python\" -Filter python.exe -Recurse -ErrorAction SilentlyContinue ^| Sort-Object FullName -Descending ^| Select-Object -First 1 -ExpandProperty FullName; if($p){$p}"') do (
    set "PYTHON=%%P"
)

if not defined PYTHON (
    echo ERROR: Python was installed but could not be located.
    echo Restart Windows and run this installer again.
    pause
    exit /b 1
)

:python_found

echo [1/4] Python found:
%PYTHON% --version
echo.

REM ------------------------------------------------------------
REM Check / install pip
REM ------------------------------------------------------------

echo [2/4] Checking pip...

%PYTHON% -m pip --version >nul 2>&1

if not %errorlevel%==0 (
    echo pip was not found. Installing pip...
    %PYTHON% -m ensurepip --upgrade

    if not %errorlevel%==0 (
        echo ERROR: pip installation failed.
        pause
        exit /b 1
    )
) else (
    echo pip is already installed.
)

echo.
echo Updating pip...
%PYTHON% -m pip install --upgrade pip
echo.

REM ------------------------------------------------------------
REM Check / install libraries
REM ------------------------------------------------------------

echo [3/4] Checking required Python libraries...
echo.

call :install_package requests
call :install_package pandas
call :install_package numpy
call :install_package yfinance

echo.
echo [4/4] Verifying installation...
echo.

%PYTHON% -c "import requests, pandas, numpy, yfinance; print('All required libraries imported successfully.')"

if not %errorlevel%==0 (
    echo.
    echo ERROR: One or more libraries could not be imported.
    echo Please review the messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo              INSTALLATION COMPLETE
echo ============================================================
echo.
echo Python, pip and all required libraries are ready.
echo.
pause
exit /b 0


:install_package
set "PACKAGE=%~1"

%PYTHON% -c "import %PACKAGE%" >nul 2>&1

if %errorlevel%==0 (
    echo [OK] %PACKAGE% is already installed.
) else (
    echo [INSTALL] %PACKAGE%
    %PYTHON% -m pip install %PACKAGE%

    if not %errorlevel%==0 (
        echo [ERROR] Failed to install %PACKAGE%.
        pause
        exit /b 1
    )
)

exit /b 0