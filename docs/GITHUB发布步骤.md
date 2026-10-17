# 一步步上传 GitHub

建议仓库名：`panda-climbing`；公开仓库。当前本地整理与远程发布是两个阶段，登录前不会创建远程仓库。

## 第一步：登录

使用 GitHub CLI 的设备授权流程，在浏览器登录自己的账号并授权。不要把密码或 Token 发到聊天里。

## 第二步：确认仓库名与提交身份

确认仓库名、公开可见性，以及提交者名称和邮箱。可以使用 GitHub Settings → Emails 中的 noreply 邮箱。

## 第三步：上传源码

先检查暂存文件，确认不含 `.config.yaml`、数据库、录音、完整 Flash、下载压缩包和编译目录；提交后创建仓库并推送 `main`。

## 第四步：启用 Pages

打开仓库 **Settings → Pages → Build and deployment → Source → GitHub Actions**。

在 **Actions → Publish project showcase → Run workflow** 手动运行一次。成功后会得到 `https://用户名.github.io/panda-climbing/`。

## 第五步：检查分享效果

使用浏览器无痕窗口及手机打开首页，分别检查报告、PDF 下载和 App 演示。GitHub Pages 不运行 Python 服务端，真实设备功能仍使用单独部署的服务。
