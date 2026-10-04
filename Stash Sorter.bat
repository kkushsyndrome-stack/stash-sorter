@echo off
rem Double-click to open Stash Sorter in your browser.
rem Runs the code in this folder when Python is installed, so it is always the version you have checked out;
rem dist\StashSorter.exe (built with tools\build_exe.py) is only used when there is no Python.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m stash_sorter %* & goto :end)
where python >nul 2>nul && (python -m stash_sorter %* & goto :end)
if exist "dist\StashSorter.exe" ( "dist\StashSorter.exe" %* & goto :end )
echo Python 3.9 or newer is needed: https://www.python.org/downloads/  (tick "Add python.exe to PATH")
:end
pause
