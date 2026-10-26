@echo off
rem Always run with this repository as the working root, so maps underneath
rem tools\maps_catalog.json, .tmp\maps_source and annotations\ are found.
set "SQUAD_MAP_ROOT=%~dp0"
cd /d "%~dp0"
if exist "%~dp0map-water-bridge-annotator.exe" (
  start "" "%~dp0map-water-bridge-annotator.exe"
  goto :eof
)
python "%~dp0tools\annotate_maps.py"
