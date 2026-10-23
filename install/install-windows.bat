@echo off
rem One-time setup on Windows: installs Material-Aware Print and adds Start menu and desktop shortcuts.
rem Double-click this file. No administrator rights needed.
rem NOTE: not yet tested on a Windows machine.
setlocal
cd /d "%~dp0\.."
set "REPO=%CD%"
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

where uv >nul 2>nul
if errorlevel 1 (
  echo Installing uv, a small Python manager...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
if not exist ".venv\Scripts\python.exe" uv venv --python 3.12 .venv
echo Installing Material-Aware Print (the first time downloads about 300 MB)...
uv pip install --python .venv\Scripts\python.exe -e .
if errorlevel 1 goto :fail

powershell -NoProfile -Command ^
  "$sh = New-Object -ComObject WScript.Shell;" ^
  "foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {" ^
  "  $s = $sh.CreateShortcut((Join-Path $dir 'Material-Aware Print.lnk'));" ^
  "  $s.TargetPath = '%REPO%\.venv\Scripts\pythonw.exe'; $s.Arguments = '-m matprint.app';" ^
  "  $s.WorkingDirectory = '%REPO%'; $s.Description = 'Adjust part models for 3D printing'; $s.Save() }"

echo.
echo Done. Open "Material-Aware Print" from the Start menu or your desktop.
pause
exit /b 0

:fail
echo Something went wrong. Please send a screenshot of this window.
pause
exit /b 1
