@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AulaBot - instalacion
echo.
echo  ============================================================
echo   AULABOT - instalando librerias
echo  ============================================================
echo.

set "PY="
for %%V in (3.12 3.11 3.10) do (
  py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V" && goto :encontrado
)
py -c "import sys" >nul 2>&1 && set "PY=py" && goto :encontrado
python -c "import sys" >nul 2>&1 && set "PY=python" && goto :encontrado

echo  No encontre Python en esta computadora.
echo.
echo  Descargalo de python.org (version 3.12, 64 bits) y al instalarlo
echo  MARCA la casilla "Add python.exe to PATH".
echo  Despues cierra esta ventana, abre otra y vuelve a correr este archivo.
echo.
pause
exit /b 1

:encontrado
echo  Usando: %PY%
%PY% -c "import sys; print('  Version:', sys.version.split()[0])"
echo.
echo  Esto se demora unos minutos. No cierres la ventana.
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
echo.
if errorlevel 1 (
  echo  Algo fallo al instalar. Copiame el error de arriba.
) else (
  echo  Listo. Ya puedes correr "correr.bat".
)
echo.
pause
