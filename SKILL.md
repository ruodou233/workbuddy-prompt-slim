---
name: workbuddy-prompt-slim
description: 精简并重放 WorkBuddy（腾讯 Electron 客户端）的内置提示词。WorkBuddy 升级后提示词被整体覆盖时、要清理或检查 WorkBuddy 内置提示词时、想知道 WorkBuddy 每次会话往模型上下文里塞了什么时使用。
---

# WorkBuddy 提示词精简（可重放）

只留三类：用户自己设计的内容、环境事实、工具接口协议。其余一律不留。精简结果存成一份配置目录 `profile/`，应用每次升级把提示词整体覆盖后，一条命令重放。

工具是 `scripts/slim.py`（纯标准库，`/usr/bin/python3` 可跑）。下面的 `slim` 指 `python3 <本 skill 目录>/scripts/slim.py`，`profile` 指 `<本 skill 目录>/profile`（已被 `.gitignore` 忽略；格式见 `profile.example/` 和 README）。

## 流程

1. **看现状**：`slim scan --profile profile`。按字符数从大到小列出所有承载提示词的文件和 `product.json` 的 prompt，并标注 一致 / 待重放 / 未审 / 保留原样。
2. **升级后重放**：
   - `slim apply --profile profile --dry-run` 先预演；
   - `slim apply --profile profile` 正式写入（原件先备份到 `~/.workbuddy-slim/backups/<时间戳>/`），报告已改、找不到目标、未审三类；
   - `slim verify --profile profile`，非零退出码就是有问题；
   - **完全退出并重启 WorkBuddy**，新开一个会话，再跑一次 `verify`，看第 ④ 项的系统消息字符数和原版特征串。
3. **第一次建配置**：手工精简完后 `slim capture --profile profile`。如果手头有原版备份目录（按绝对路径镜像、只含被改过的文件），加 `--baseline <备份目录>`，只存改过的项，其余记进 `kept.txt`，这样没改过的工具说明书升级后仍跟随新版。
4. **出问题**：`slim restore ~/.workbuddy-slim/backups/<时间戳>`。

先用 `--bundle`、`--home` 指向一份临时副本演练，确认无误再对真实安装执行。

## 遇到「未审」的新文件或新 prompt

逐个读，按这个标准判断：

- **保留**：用户自己设计的内容（共享记忆入口、自定义指令）；环境事实（工作目录、平台、日期、是否 Git 仓库、响应语言等）；工具接口协议（工具说明书、子代理通信格式、程序依赖的返回格式、记忆块的透传变量）。
- **删除**：平台人格、营销与引导、安全套话、工作流说教、风格模板。
- **删环境字段前先确认**：它是不是运行时事实、有没有别的模板还依赖它。作者实测出过事故：一次清理误删了「当前目录是否为 Git 仓库」，Agent 之后一直不知道自己在不在仓库里。
- 决定后把改好的文本放进（或重新 `capture` 进）配置目录；决定原样保留的，写进 `kept.txt`（`file:<路径键>` 或 `prompt:<名字>`）。
- 新增的变量或标签如果必须保留，补进 `profile/required.txt`。

## 三个坑

1. **必须重启**：`product.json` 只在应用启动时读入内存。改完不完全退出再启动，运行中的进程（包括子代理那一路）仍用旧提示词；作者实测：改完没重启的那几个小时里，子代理调用全程还在用约 16K 字符的原版提示词。
2. **不能清空 SOUL.md、IDENTITY.md**：内容为空会被应用自动重建成默认模板。要精简就写一行最小内容。`apply` 遇到配置里内容为空的这类文件会直接跳过。
3. **别误删环境字段**：见上，尤其是 Git 仓库、工作目录这类。`verify` 的 `required.txt` 检查就是兜这个底的，新增必留项要记得补。

另外：不要去改 `app.asar` 本体（有完整性校验），只改 `app.asar.unpacked` 和 `~/.workbuddy` 下的文件；改应用包会破坏代码签名，应用更新会整体覆盖，所以要重放。
