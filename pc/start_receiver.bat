@echo off
rem Waits for captures from the iPhone and saves .blend files in Documents\Trackshot.
rem If Blender is installed somewhere else or is a newer version, change this path.
set BLENDER=C:\Program Files\Blender Foundation\Blender 5.0\blender.exe

"%BLENDER%" --background --factory-startup --python "%~dp0trackshot_blender.py" -- serve --out "%USERPROFILE%\Documents\Trackshot"
pause
