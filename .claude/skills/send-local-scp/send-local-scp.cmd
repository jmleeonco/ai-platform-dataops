@echo off
rem send-local-scp wrapper for cmd / PowerShell. Example:
rem   .claude\skills\send-local-scp\send-local-scp.cmd send D:\data\case01 --modality CT
python "%~dp0scripts\send_local_scp.py" %*
