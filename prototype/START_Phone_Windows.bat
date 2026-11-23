@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 goto usepy
where python >nul 2>nul
if %errorlevel%==0 goto usepython
echo.
echo Python is not installed.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
echo Then double-click this file again.
goto done
:usepy
py -3 launch.py --phone
goto done
:usepython
python launch.py --phone
:done
echo.
pause
