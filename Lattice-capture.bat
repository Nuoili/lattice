@echo off
rem Lattice - capture current window layout into lattice.json
"%~dp0Lattice.exe" --capture --save
echo.
pause
