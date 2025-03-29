@echo off
REM Windows batch script to launch token.place

setlocal enabledelayedexpansion

REM Detect Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Python not found in PATH.
    echo Please install Python 3.8 or higher and make sure it's in your PATH.
    exit /b 1
)

REM Set environment variables
set "TOKEN_PLACE_ENV=development"
set "PLATFORM=windows"

REM Parse arguments
set "ACTION=start"
set "COMPONENT=all"
set "PORT="
set "ENV=%TOKEN_PLACE_ENV%"

:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="start" (
    set "ACTION=start"
    shift
    goto :parse_args
)
if /i "%~1"=="stop" (
    set "ACTION=stop"
    shift
    goto :parse_args
)
if /i "%~1"=="restart" (
    set "ACTION=restart"
    shift
    goto :parse_args
)
if /i "%~1"=="status" (
    set "ACTION=status"
    shift
    goto :parse_args
)
if /i "%~1"=="--component" (
    set "COMPONENT=%~2"
    shift
    shift
    goto :parse_args
)
if /i "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto :parse_args
)
if /i "%~1"=="--env" (
    set "ENV=%~2"
    shift
    shift
    goto :parse_args
)
shift
goto :parse_args

:done_args

REM Set environment variables
set "TOKEN_PLACE_ENV=%ENV%"

REM Build command
set "CMD=python -m scripts.launcher %ACTION% --component %COMPONENT%"
if defined PORT (
    set "CMD=!CMD! --port %PORT%"
)
if defined ENV (
    set "CMD=!CMD! --env %ENV%"
)

REM Run the command
echo Running: !CMD!
!CMD!

exit /b %ERRORLEVEL% 