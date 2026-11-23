@echo off
rem Windows equivalent of rodin.sh: rodin.cmd submit ^| collect ^| inspect ^| preview ^| bind ...  (see README "Windows")
setlocal
set "HERE=%~dp0"
if "%HYPER3D_VENV%"=="" set "HYPER3D_VENV=%USERPROFILE%\.cache\hyper3d-venv"
"%HYPER3D_VENV%\Scripts\python.exe" -X utf8 "%HERE%rodin_backend.py" %*
exit /b %ERRORLEVEL%
