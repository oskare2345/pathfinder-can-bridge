@echo off
rem Pathfinder CAN bridge (unofficial) - double-click to open the window.
cd /d "%~dp0"
py -c "import can, serial" 2>nul || (
    echo Installing python-can and pyserial ...
    py -m pip install -r requirements.txt
)
start "" pyw pathfinder_gui.py