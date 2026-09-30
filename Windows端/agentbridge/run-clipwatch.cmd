@echo off
rem Clipwatch launcher (login startup: Clipwatch.lnk -> run-clipwatch-hidden.vbs -> this).
rem Idempotency is handled inside clipwatch.py via named mutex; a second instance exits by itself.
set PY=D:\Python 3.12.4\pythonw.exe
set APP=D:\KEEPPER\_filebridge\clipwatch.py

start "" "%PY%" "%APP%"
