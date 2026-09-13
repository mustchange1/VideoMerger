@echo off
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run_vm_automatic.ps1"
if errorlevel 1 pause
