# workbuddy-prompt-slim

给 WorkBuddy（腾讯的 Electron 桌面客户端）的内置提示词做「精简 + 可重放」。

## 它解决什么问题

WorkBuddy 每次会话都会往模型上下文注入大量平台提示词：人格设定、安全条款、工作流说教、引导和营销文案，外加各种模板片段。想只留下自己设计的内容、环境事实和工具接口协议，就得去改这些文件。

难点在于应用一升级，所有这些文件被整体覆盖，手工改的全部作废。这个工具把「改成什么样」存成一份配置目录，升级之后一条命令重放，并做验证。

作者自己的实测（WorkBuddy 5.9.3）：主会话系统提示词从约 19,600 字符压到约 550 字符；另外每次会话会整份注入 SOUL.md、IDENTITY.md 两份默认人格模板（约 2,600 字符），技能列表里也混着几十个推荐、引导类内置技能。这些都可以写进配置，一次处理、升级后重放。

提示词分布在这几处，工具都会处理：

| 处 | 位置 |
| --- | --- |
| 应用包 | `app.asar.unpacked/cli/product.json` 的 `prompts[]`；`resources/templates/**/*.tpl`；`resources/plugins/workbuddy-builtin/{welcomemode,prompt-common,interactionmode}/**` 下的片段 |
| 插件缓存 | `~/.workbuddy/plugins/cache/workbuddy-builtin/<插件>/<版本目录>/`（运行时优先读这里，只处理与当前应用版本匹配的版本目录） |
| 市场目录 | `~/.workbuddy/plugins/marketplaces/workbuddy-builtin/` |
| 产品配置缓存 | `~/.workbuddy/cache/acc-product-config-v3*.json`、`conversation-product-spill/*.json`（嵌着 prompts 副本，按名字同步替换） |
| 个人文件 | `~/.workbuddy/user-*-personal/USER.md`，以及 `settings.json` 的 `skillOverrides`（只合并这一个键） |

## 用法

需要 macOS 自带的 `/usr/bin/python3`（3.9 即可），只用标准库。`node` 可选，有的话 `verify` 会用应用自带的 nunjucks 做真实编译。

```bash
S=scripts/slim.py

# 1. 看现状：按字符数从大到小
python3 $S scan --profile profile

# 2. 第一次：手工精简好之后，把当前状态存成配置目录
python3 $S capture --profile profile
#    有原版备份目录时（按绝对路径镜像、只含被改过的文件），只存改过的项：
python3 $S capture --profile profile --baseline /path/to/backup

# 3. 升级之后：预演 -> 重放 -> 验证
python3 $S apply  --profile profile --dry-run
python3 $S apply  --profile profile
python3 $S verify --profile profile

# 4. 完全退出并重启 WorkBuddy（product.json 只在启动时读入），新开会话后再 verify 一次

# 5. 反悔：按备份清单还原
python3 $S restore ~/.workbuddy-slim/backups/<时间戳>
```

所有根路径可以覆盖，方便在副本上演练：`--bundle`（应用包解包目录）、`--home`（数据目录，默认 `~/.workbuddy`）、`--backup-root`（备份根目录）。

### 子命令

- `scan`：列出所有承载提示词的文本文件和每个 prompt 的字符数；给了 `--profile` 就标出 一致 / 待重放 / 未审 / 保留原样。
- `capture`：把当前状态存成配置目录（见下）。必留清单 `required.txt` 已存在时不覆盖，保留你的手改。
- `apply`：内容相同的跳过；要改的先整批备份再写入；最后报告已改、配置里有但当前版本找不到目标的、当前版本有但配置里没有的（未审）。
- `verify`：① 全部模板和片段试编译（优先用应用自带的 nunjucks，找不到 node 就退化为标签配对检查），并检查 `include` 指向的片段存在；② `required.txt` 每一项都还在；③ 配置与当前安装一致（升级后忘了 `apply` 会在这里露出来）；④ 读 `~/.workbuddy/traces` 里最近一次生成的输入，报告系统消息字符数，检查有没有原版特征串。有问题返回非零退出码。
- `restore`：按备份目录里的 `manifest.json` 把原件放回去，`apply` 新建的文件会被删掉。

### 配置目录

```
profile/
  bundle/<相对应用包的路径>          文本文件，按「处 + 与版本无关的路径」存放
  cache/<插件名>/<版本目录内的路径>   不含版本目录名，换版本也能对上
  market/<相对市场目录的路径>
  prompts.json                      product.json 的 prompt：名字 -> 文本
  skill-overrides.json              settings.json 的 skillOverrides
  USER.md                           个人工作区的 USER.md
  required.txt                      必须保留的变量名或 <标签>，capture 自动生成初始值，可手改
  kept.txt                          （可选）审过、决定保留原样的项
  markers.txt                       （可选）verify 额外检查的特征串，一行一个
```

配置目录里是你自己精简后的文本，可能含有原版提示词的片段，所以仓库默认忽略 `profile/`，请不要提交。格式示例见 `profile.example/`。

## 自动重放（模板，未安装）

`scripts/watch-update.sh` 依次跑 `apply`、`verify`，再用 `osascript` 发一条通知（「已重放，请重启 WorkBuddy 生效」或「验证失败，看日志」）。`scripts/com.local.workbuddy-prompt-slim.plist.template` 是对应的 launchd 模板，用 `WatchPaths` 监视 `Info.plist`。仓库里只有模板，不会替你安装。想启用的话：

```bash
sed -e "s#__SCRIPT__#$PWD/scripts/watch-update.sh#" \
    -e "s#__PROFILE__#$PWD/profile#" \
    -e "s#__HOME__#$HOME#" \
    scripts/com.local.workbuddy-prompt-slim.plist.template \
    > ~/Library/LaunchAgents/com.local.workbuddy-prompt-slim.plist
mkdir -p ~/.workbuddy-slim/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.workbuddy-prompt-slim.plist
```

停用：`launchctl bootout gui/$(id -u)/com.local.workbuddy-prompt-slim`。日志在 `~/.workbuddy-slim/logs/`。

## 风险

- **改应用包会破坏代码签名。** 已安装的应用照常运行，但这是对应用文件的修改，由你自己承担。
- **后台自动重放写不了应用包。** macOS 的「App 管理」权限不允许 launchd 拉起的进程修改 `/Applications` 里的应用（作者实测）。自动重放会先把 `~/.workbuddy` 里的部分（插件缓存、产品配置缓存，运行时优先读这里）写好，应用包内的项报告为「没有写入权限」并发通知，再在有权限的终端或 Agent 会话里跑一次 `apply` 补上。不建议为此给 `/usr/bin/python3` 或 `bash` 授予「App 管理」，那等于让所有脚本都能改你的应用。
- **应用更新会整体覆盖这些文件**，所以才需要重放。插件缓存的新版本目录可能要等应用启动一次才生成，升级后若 `apply` 提示找不到匹配版本，启动一次应用再跑一遍。
- **不要去改 `app.asar` 本体**，它有完整性校验。本工具只碰 `app.asar.unpacked` 和 `~/.workbuddy` 里的文本。
- **改完必须完全退出并重启 WorkBuddy**：`product.json` 只在启动时读入内存，不重启就仍是旧提示词。
- 不能把 SOUL.md、IDENTITY.md 清空：内容为空会被应用重建成默认模板，写一行最小内容。
- 删除「环境事实」类字段（工作目录、是否 Git 仓库等）之前要确认，`required.txt` 就是用来兜这个底的。
- 工具对每次写入都先备份，但备份只覆盖被改动的文件；批量同步 `conversation-product-spill` 时备份会比较大。

## 许可

MIT，见 `LICENSE`。本项目与腾讯无关，不包含也不分发任何 WorkBuddy 的提示词原文。
