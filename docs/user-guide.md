# 使用指南

## 本地运行

安装 Python 3.12、uv、Node.js、pnpm 与 just，然后执行：

```bash
just setup
just dev
```

浏览器打开 `http://127.0.0.1:5173`。首次使用先创建媒体库，填写现有影片目录，再扫描。

## Docker

```bash
cp .env.example .env
MEDIA_PATH=/path/to/media docker compose up -d --build
```

容器内媒体根目录是 `/media`。创建媒体库时应使用容器路径，而不是宿主机路径。

## 识别方式

- 有番号的 JAV：文件名保留番号，例如 `SSIS-123.mp4`。
- FC2、HEYZO 与无码日期号：支持常见文件名格式。
- 国产长尾或无番号内容：用标题搜索，或把作品详情页 URL 填入人工识别框。
- 欧美场景：配置 ThePornDB Token 后可使用 oshash；也可用标题、来源 URL 或 provider ID。
- 不确定结果：候选停留在审核队列，确认后才创建正式作品。

公开网页可能出现地区限制、登录要求或反自动化挑战。来源失败会单独记录，不会阻断其它来源；项目不尝试绕过访问控制。

## 整理与 NFO

整理分为“计划”和“执行”两步。计划显示移动、复制或硬链接目标及冲突；执行时必须带回计划令牌。媒体文件默认不覆盖，NFO 通过临时文件原子替换。生成内容兼容 Kodi、Jellyfin 与 Emby 常用 movie NFO 字段。

## 数据边界

Shadow MDC 只管理用户已有本地媒体的元数据与公开图片，不提供影片下载、磁力搜索、破解、登录绕过或在线播放。
