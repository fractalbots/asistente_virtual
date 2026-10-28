@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AulaBot

set "PY="
for %%V in (3.12 3.11 3.10) do (
  py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V" && goto :encontrado
)
py -c "import sys" >nul 2>&1 && set "PY=py" && goto :encontrado
python -c "import sys" >nul 2>&1 && set "PY=python" && goto :encontrado

echo  No encontre Python. Corre primero "instalar.bat".
pause
exit /b 1

:encontrado
%PY% aulabot.py %*
echo.
pause
