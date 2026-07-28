@echo off
cd /d "%~dp0"
echo ==========================================
echo Instalando dependencias do Atlas...
echo ==========================================

if not exist .venv\Scripts\python.exe (
    echo Criando ambiente virtual .venv...
    py -m venv .venv
)

echo Atualizando pip...
.\.venv\Scripts\python.exe -m pip install --upgrade pip

echo Instalando requirements.txt...
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

echo Testando ReportLab...
.\.venv\Scripts\python.exe -c "import reportlab; print('ReportLab instalado OK:', reportlab.Version)"

if errorlevel 1 (
    echo.
    echo ERRO: o ReportLab nao foi instalado corretamente.
    echo Tente rodar manualmente:
    echo .\.venv\Scripts\python.exe -m pip install reportlab
    pause
    exit /b 1
)

echo.
echo Dependencias instaladas com sucesso.
echo Agora rode RODAR_SISTEMA_WINDOWS.bat ou: .\.venv\Scripts\python.exe app.py
pause
