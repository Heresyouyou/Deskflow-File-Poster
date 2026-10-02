@echo off
rem Deskflow-autostart launcher (login startup: Deskflow Autostart.lnk -> run-autostart-hidden.vbs -> this).
rem Idempotency is handled inside deskflow_autostart.py via named mutex; a second instance exits by itself.
set PY=D:\Python 3.12.4\pythonw.exe
set APP=D:\KEEPPER\_filebridge\deskflow_autostart.py

start "" "%PY%" "%APP%"