# 参与开发

欢迎通过 GitHub Issue 报告问题或提出改进。提交前先搜索现有 Issue，并注明 AstrBot、NapCat、Python 和插件版本。涉及自动推送时，请说明任务类型、计划时间、消息读取日期与时段，以及任务卡片显示的最近结果。请只提供可复现问题所需的信息，不要上传真实聊天记录、QQ Cookie、SMTP 授权码、数据库或完整配置文件。

## 本地检查

在仓库根目录运行：

```sh
python3 -m unittest discover -s tests -q
node --check pages/auto-summary/app.js
node --check pages/auto-summary/settings.js
python3 scripts/build_release.py --check
```

Python 测试不依赖正在运行的 AstrBot 或 NapCat。若修改了消息读取、补读或推送流程，还应在自己的 AstrBot + NapCat 环境中验证，并在 PR 中写清实际测试范围；自动化测试不能证明 QQ 历史已完整返回。

## 提交变更

- 一次 PR 尽量解决一个明确问题，说明触发条件、修改后的行为和验证结果。
- 修改用户可见行为时同步更新 `README.md`；发布新版本时同步更新 `metadata.yaml`、README 标题和 `CHANGELOG.md`。
- 测试用例使用合成群号、邮箱和聊天内容，不提交个人数据或运行时生成的文件。

提交代码即表示你有权按本项目的 [GPL-3.0-only 许可证](LICENSE)提供这些内容。不要提交来自其他项目、但许可证不兼容或来源不明的代码。
