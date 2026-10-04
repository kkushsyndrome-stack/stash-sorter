@echo off
rem Double-click to open Stash Sorter in your browser.
cd /d "%~dp0"
if exist "dist\StashSorter.exe" ( "dist\StashSorter.exe" %* & goto :end )
where py >nul 2>nul && (py -3 -m stash_sorter %* & goto :end)
where python >nul 2>nul && (python -m stash_sorter %* & goto :end)
echo Python 3.9 or newer is needed: https://www.python.org/downloads/  (tick "Add python.exe to PATH")
:end
pause
