@echo off
chcp 65001 >nul
title 酸菜鱼翻译器
cd /d "%~dp0"

echo.
echo   酸菜鱼翻译器  SuanCaiYu Translator
echo   直播实时翻译 - 把外语直播变成中文字幕
echo   ------------------------------------------------
echo.

rem 依次尝试 py 启动器、python 命令、常见安装路径
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 main.py
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python main.py
    goto done
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" main.py
    goto done
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" main.py
    goto done
)

echo [错误] 没有找到 Python。
echo.
echo   请先安装 Python 3.10 或更高版本： https://www.python.org/downloads/
echo   安装时记得勾选 "Add Python to PATH"。
echo.
echo   装好后再运行本文件，或者手动执行：
echo       python -m pip install -r requirements.txt
echo       python main.py
echo.
pause
exit /b 1

:done
rem 如果程序异常退出，留住窗口让用户看到错误信息
if errorlevel 1 (
    echo.
    echo [提示] 程序退出时返回了错误码，上面应该有具体原因。
    pause
)
