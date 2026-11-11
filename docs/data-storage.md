# 代码与运行数据边界

代码仓库保存模型、算法、加载与迁移实现，以及验证行为必需的精简离线夹具。
标签词典、分类词条、演员别名、DMM 前缀、人工名单、种子人物与作品、创作者和站点映射均属于运行数据，
不得放入 Python 常量或源码目录，也不随 Python 发布包或镜像分发。

## 外部文件

`Settings.reference_data_dir` 指向参考数据目录，默认 `<data_dir>/reference`。
沿用配置文件方式；兼容入口 `SHADOW_MDC_REFERENCE_DATA_DIR` 仅承载目录路径。
实际文件保存在持久数据卷或仓库外的受限目录，随片库备份；目录建议 0700、文件 0600。

| 文件 | 用途 | 未提供时 |
| --- | --- | --- |
| `tag-dictionary.seed.json` | 首次词典、别名、显隐和展示规则 | 创建空词典，来源标签仍可维护 |
| `classifications.seed.json` | 作品分类展示信息及初始分类词条 | 仅生成结构必需的待补项与来源体系投影 |
| `actress_name_map.json.gz` | 演员名称别名映射 | 使用原名和通用姓名归一化 |
| `dmm_content_id_prefixes.json.gz` | 系列到内容前缀的映射 | 使用通用内容 ID 候选算法 |
| `catalog-rules.json` | 机构、识别提示、演员保留名单、维护种子与站点映射 | 无人工名单或种子注入，保留通用算法 |

JSON 结构由 `TagDocument`、`ClassificationSeed` 和 `CatalogRules` 定义；两个 gzip 文件解压后分别为
`dict[str, str]` 和 `dict[str, tuple[str, ...]]`。存在但格式错误的文件会报错，不静默替换为空数据。
核心模块导入不读取这些文件；加载按数据目录隔离，文件替换后刷新缓存。独立维护脚本启动时加载所需数据。

实际标签词典保存在 `<data_dir>/tag-dictionary.json`，既有文件优先于种子，重启或升级不覆盖人工修改。
分类词条继续保存数据库，迁移旧分类时以数据库当前名称与别名为准。
本轮涉及的维护脚本同样使用配置的 `data_dir`，不把目录数据或映射写回源码。

## 从旧版本保全数据

下次部署前由新版本工具读取上一版本，将缺少的数据迁入外部持久目录。通用调用方式：

```bash
uv run python scripts/migrate_reference_data.py \
  --legacy-root /srv/shadow-mdc/previous \
  --output /srv/shadow-mdc/shared/data/reference
```

工具复制四份原始数据文件，并只解析旧 Python 文件中的字面量数据，不执行旧代码。
已有目标文件一律保留，新增文件以完整内容原子发布并限制权限；不会触碰作品、数据库或现有运行词典。
NAS 部署脚本在完成一致备份后、切换版本前调用此工具，仍按原部署规则验证与保留版本。
自定义参考数据目录必须在部署命令与服务配置中使用同一路径。首次安装可以不提供参考数据。

重建演员映射的 `scripts/build_actress_name_map.py` 也写入外部参考目录。数据来源和许可证见 `NOTICE`；
更新资料通过独立数据维护流程进行，不将生成结果提交代码仓库。

测试固定使用 `tests/fixtures/reference-data` 中的少量回归输入，不读取私人目录、不要求完整片库。
