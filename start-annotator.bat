@echo off
chcp 65001 >nul
setlocal
rem One-click launcher: always run with this folder as the working root, so
rem tools\maps_catalog.json, .tmp\maps_source and annotations\ are found.
set "SQUAD_MAP_ROOT=%~dp0"
cd /d "%~dp0"

if not exist "%~dp0tools\maps_catalog.json" (
  echo [!] tools\maps_catalog.json not found.
  echo     Put this .bat in the repo root ^(next to tools\^).
  pause
  exit /b 1
)

rem First run: the annotator needs the map images. -- about 175 MB, one time.
if not exist "%~dp0.tmp\maps_source\yehorivka\heightmap.png" (
  echo.
  echo [!] 还缺地图数据（约 175 MB，只需下载一次，用 Windows 自带的 PowerShell）。
  echo.
  choice /c YN /t 20 /d Y /m "现在下载吗（20 秒后自动开始）"
  if errorlevel 2 goto :nodata
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\download_maps.ps1"
  if errorlevel 1 (
    echo.
    echo [!] 下载没完成，再双击一次这个 bat 会接着下。
    pause
    exit /b 1
  )
)

if exist "%~dp0map-water-bridge-annotator.exe" (
  start "" "%~dp0map-water-bridge-annotator.exe"
  goto :eof
)
echo [i] 没找到 exe，改用 Python 源码启动（需要 pip install pillow numpy）。
python "%~dp0tools\annotate_maps.py"
goto :eof

:nodata
echo.
echo 已跳过下载。之后可以手动执行：
echo     powershell -ExecutionPolicy Bypass -File "%~dp0tools\download_maps.ps1"
pause
exit /b 1
