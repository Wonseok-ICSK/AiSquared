@echo off
rem Double-click launcher for AiSquared Travel Planner: starts the server and opens Edge,
rem or just opens Edge if it is already running. Closing the server window stops price checks.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" %*
