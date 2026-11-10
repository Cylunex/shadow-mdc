# MDC operator skill

`skills/mdc-operator/SKILL.md` 提供片库日常操作说明，`scripts/mdc` 为配套 CLI。
也可通过已安装包的 `shadow-mdc-skill` 调用。新能力使用 HTTP/CLI，不需要 MCP 进程或注册。

## 能力

| 能力 | 效果 |
| --- | --- |
| `system.health` | 检查服务 |
| `works.query` / `works.facets` / `works.detail` | 查询作品、筛选计数和详情 |
| `actors.query` | 演员轻量分页查询 |
| `tags.list` | 带封面的大标签及作品数 |
| `classifications.list` / `classifications.work` | 七维分类字典和作品当前分类 |
| `classifications.assign` | 设置明确指定的分类维度，省略字段保持原值 |
| `works.want` | 设置或取消真实作品的想看状态 |
| `playlists.list` / `playlists.works` | 查询指定空间的人工与动态片单 |
| `tasks.list` / `tasks.get` | 读取执行状态和结果 |

每项能力的真实参数通过 `catalog` 或 `describe` 读取。查询有分页时保留原筛选并沿游标或 offset 翻页。
片单空间来自配置的 `owner`；想看沿用个人片库的共同列表。写入结果未知时先读取目标，核对后再决定是否重试。
本 skill 不提交下载、不执行文件删除或对外发送，也不管理服务部署。

## 连接

私有 JSON 配置可使用 `--config`、`MDC_SKILL_CONFIG` 文件路径，或 skill 目录下已忽略的 `skill.local.json`。
配置和凭据文件保持 0600，实际值不提交 Git。HTTP 配置结构：

```json
{"transport":"http","base_url":"https://example.com","token_file":"REPLACE_TOKEN_FILE","owner":"REPLACE_CLIENT_ID","timeout_seconds":60}
```

CLI 也支持已有 SSH 别名和远端绝对启动器、配置路径；输入 JSON 通过 stdin 传递，不拼入远端命令。
结构见 skill 的 [常见用法](../skills/mdc-operator/references/workflows.md)。本机可通过 SSH 使用服务端配置，无需复制 Token。

## 服务接口与边界

- `GET /api/skill/v1/catalog` 返回固定能力目录、读写效果和输入 schema。
- `POST /api/skill/v1/call` 接收 `name` 和 `arguments`，返回 `ok/capability/status/result`。
- 两个入口都复用已有机器服务凭据进行 Bearer 校验，未配置返回 503，匿名及错误凭据返回 401。
- 旧配置项 `mcp_token` 保持原名；新接口不使用 MCP 协议，不注册新 MCP 工具。
- 服务端只转发固定能力对应的域内端点，不接受任意 URL、HTTP 方法或路径。
- 原页面的作品详情使用前端偏好覆盖；skill 详情合并实际想看状态，便于写后核验。
- CLI 不跟随 HTTP 重定向，不输出凭据、连接配置正文或原始 SSH 错误，不自动重放写入。

普通授权的分类与想看修改直接执行，使用实际作品 ID 和正式分类 ID，完成后读取核验。
