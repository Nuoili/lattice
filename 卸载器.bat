@echo off
chcp 936 >nul
setlocal
title Lattice 卸载器
echo ==============================================
echo   Lattice 卸载器
echo ==============================================
echo.
echo   本操作将：
echo     1. 结束 Lattice.exe 进程
echo     2. 移除开机自启（Startup\Lattice-startup.bat）
echo     3. 将 D:\Lattice 整个文件夹移入回收站
echo.
echo   代码已备份在 GitHub（Nuoili/lattice），可随时找回。
echo.
choice /c YN /m "确认卸载吗 [Y=卸载 / N=取消]"
if errorlevel 2 goto :cancel

taskkill /f /im Lattice.exe >nul 2>&1
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Lattice-startup.bat" >nul 2>&1

set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 2; Add-Type -AssemblyName Microsoft.VisualBasic; foreach($i in 1..5){ try{ [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory('%APPDIR%','OnlyErrorDialogs','SendToRecycleBin'); break }catch{ Start-Sleep 2 } }"

echo   进程已结束、自启已移除。
echo   文件夹即将移入回收站，本窗口可关闭。
echo   （若 D:\Lattice 仍存在，说明有文件被占用，稍后重跑本卸载器即可）
ping -n 4 127.0.0.1 >nul
exit

:cancel
echo   已取消，未做任何更改。
ping -n 3 127.0.0.1 >nul
exit
