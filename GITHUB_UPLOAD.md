# 将 CommerceFlow 上传到 GitHub

目标仓库：<https://github.com/zmjj-kk/CommerceFlow.git>。

以下命令在 Windows PowerShell 执行。项目目前没有 `.git`，因此需要先初始化。项目展示名称已经改为 CommerceFlow，本机目录位置仍保持原样。

## 1 进入目录并初始化

```powershell
Set-Location -LiteralPath "C:\Users\Lenovo\Desktop\MEWHELP python"
git init -b main
```

如果已经初始化过，不需要重复初始化；通过 `git status` 检查即可。

## 2 检查待上传内容

```powershell
git add -A
git --no-pager diff --cached --name-only
git --no-pager diff --cached --stat
```

确认没有 `.env`、虚拟环境、数据库、运行日志、模型权重或私有备份。`.env.example` 是公开模板，可以提交。Word 笔记和本地渲染目录已加入忽略规则，仍保留在本机。

## 3 提交

首次提交如果尚未配置作者信息，先设置当前项目的 Git 身份：

```powershell
git config user.name "zmjj-kk"
git config user.email "替换为你的GitHub邮箱或GitHub提供的noreply邮箱"
```

然后提交：

```powershell
git commit -m "Initial release of CommerceFlow"
```

## 4 关联仓库

首次关联：

```powershell
git remote add origin https://github.com/zmjj-kk/CommerceFlow.git
git remote -v
```

如果提示 `remote origin already exists`，改用：

```powershell
git remote set-url origin https://github.com/zmjj-kk/CommerceFlow.git
```

GitHub 目标仓库应是空仓库，不要额外初始化 README、License 或 `.gitignore`。本地已经提供了 README 和忽略规则，原始来源署名也予以保留。

## 5 登录并推送

如果仍使用本机 `127.0.0.1:7890` 代理，保持代理软件运行，使用一次性参数推送：

```powershell
git.exe -c http.proxy=http://127.0.0.1:7890 -c credential.username=zmjj-kk push -u origin main
```

如果不需要代理，使用：

```powershell
git push -u origin main
```

认证失败时，通过已安装的 Git Credential Manager 重新登录：

```powershell
git.exe -c http.proxy=http://127.0.0.1:7890 credential-manager github login --username zmjj-kk --browser --force
```

在浏览器中登录 `zmjj-kk` 并授权，返回终端后再次执行推送命令。GitHub 账号密码不能作为 HTTPS Git 密码使用；浏览器登录完成后由凭证管理器处理 Token。

如果远端已有初始提交而提示 `non-fast-forward`，先检查并合并远端内容，不要直接强制推送。网络超时、认证失败和提交冲突是不同问题，需要分别处理。

## 后续更新

```powershell
git add -A
git --no-pager diff --cached --stat
git commit -m "Describe the change"
git.exe -c http.proxy=http://127.0.0.1:7890 -c credential.username=zmjj-kk push
```

推送成功后，根目录 README 会展示在仓库首页。本次本地整理不会自动创建 GitHub 仓库、上传代码或改变你的 GitHub 账户认证状态。
