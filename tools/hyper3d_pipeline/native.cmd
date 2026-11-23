@echo off
rem Windows equivalent of native.sh: native.cmd login ^| inspect ^| prepare ...  (see README "Windows")
setlocal
set "HERE=%~dp0"
if "%HYPER3D_VENV%"=="" set "HYPER3D_VENV=%USERPROFILE%\.cache\hyper3d-venv"
"%HYPER3D_VENV%\Scripts\python.exe" -X utf8 "%HERE%native_browser.py" %*
exit /b %ERRORLEVEL%
