@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Upload to GitHub

where git >nul 2>nul
if errorlevel 1 goto NOGIT

echo ============================================================
echo   上传当前项目到 GitHub
echo ============================================================
echo.

echo   1) 检查提交身份...
git config user.name >nul 2>nul
if errorlevel 1 goto ASKNAME
git config user.email >nul 2>nul
if errorlevel 1 goto ASKMAIL
goto PREP

:ASKNAME
set /p GITNAME=     GitHub 用户名：
git config user.name "!GITNAME!"
goto PREP

:ASKMAIL
set /p GITMAIL=     GitHub 邮箱：
git config user.email "!GITMAIL!"

:PREP
set "REPO="
for /f "delims=" %%u in ('git remote get-url origin 2^>nul') do set "REPO=%%u"
echo.
set /p NEWREPO=  2) 仓库地址 [!REPO!]（直接回车用默认）：
if not "!NEWREPO!"=="" set "REPO=!NEWREPO!"
if "!REPO!"=="" goto EMPTY

echo.
echo   3) 暂存文件（约 200MB，稍等）...
git add -A

echo.
echo   4) 创建提交...
git commit -m "init: wxbot"
git rev-parse --verify HEAD >nul 2>nul
if errorlevel 1 goto NOCOMMIT

echo.
echo   5) 设置分支与远程...
git branch -M main
git remote get-url origin >nul 2>nul
if errorlevel 1 git remote add origin "!REPO!"
git remote set-url origin "!REPO!"

echo.
echo   6) 开始推送（首次会弹出浏览器登录 GitHub）...
echo.
git push -u origin main
if errorlevel 1 goto PUSHFAIL

echo.
echo   [OK] 上传完成！去 https://github.com/chadaidai01/wxbot 刷新看看。
echo.
pause
exit /b 0

:NOCOMMIT
echo.
echo   [x] 提交失败（上面 git commit 的输出就是原因），常见原因：
echo       - user.name / user.email 没设置
echo       - 文件全部被 .gitignore 忽略（用 git status 看）
echo.
pause
exit /b 1

:PUSHFAIL
echo.
echo   [x] 推送失败，看上面 git 的报错，常见原因：
echo       - 网络连不上 github（国内请开代理/VPN 后重试）
echo       - rejected / fetch first：仓库不是空的（建仓库时勾了 README），
echo         删掉该仓库重建一个空仓库，再运行本脚本
echo       - 登录窗口被取消 / 账号无权限
echo.
echo   已提交的内容不会丢，重跑本脚本即可续传。
echo.
pause
exit /b 1

:NOGIT
echo.
echo   [x] 没有检测到 Git，请先安装：https://git-scm.com/download/win
echo.
pause
exit /b 1

:EMPTY
echo.
echo   [x] 仓库地址不能为空。
echo.
pause
exit /b 1