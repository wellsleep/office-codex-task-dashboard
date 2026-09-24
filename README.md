# Codex 定时任务运行看板

手机优先的只读结果汇总。Mac 每 **30 分钟**读取本机 Codex 任务及运行结果，将精简 JSON 发布到 GitHub Pages。没有后端数据库、模型 API 或前端第三方依赖。

## 页面与更新

- 最近 24 小时结果、各任务最新状态、异常队列、近七天趋势。
- 按任务、结果和时间范围查询历史；默认七天，可查看九十天或自定义日期。
- 有变化即推送；没有变化每小时发布一次采集心跳。GitHub 构建与缓存会产生额外延迟。
- 超过两小时未更新显示同步过期。Mac 休眠期间不能采集，恢复后补采。
- 只读看板不支持触发重跑、修改原任务或远程执行命令。

## 来源与兼容性

`scripts/codex_source.py` 是唯一的 SQLite/配置发现适配层，启动时检查实际字段，而不依赖某个固定 Codex 版本号。

每次重新扫描 `$HOME/.codex/automations/*/automation.toml`，新任务、名称/周期/状态变化自动反映到下一次采集。新任务尚未执行时也会展示。

运行发现依次合并：

1. `automation_runs` 中的明确关联。
2. 根会话首条用户消息的 `Automation ID` 标识，补充静默或没有 inbox 的运行。
3. 原对话任务目标会话中的 `<heartbeat><automation_id>` 触发标识，按轮次采集。

普通聊天、审批子代理、工具输出和提示词不作为运行结果。任务 ID 重用时按创建时间隔离，避免把已删除任务的记录算给新任务。删除任务会从当前看板移除；本版本只监控当前配置中的任务。

运行标识使用任务 ID、会话 ID、启动时间，与发现来源无关，重复扫描不会重复计数。原对话普通跟进轮次不会混入定时记录。普通独立运行会话中的重试/跟进则显示该会话最新结果，可能需要人工确认整体状态。

字段新增不影响采集；运行索引缺失时尝试会话标识回退，并显示覆盖警告。关键会话格式不兼容、配置无法完整读取或所有发现来源均不可用时停止发布，保留上次有效快照；源文件暂时缺失时保留带“来源未复核”标记的已有历史。新增的、未识别的触发格式不做猜测，显示回执缺失，后续只需扩展适配器及测试。

Codex 内部文件不是稳定公共接口，无法保证未知未来版本零改动兼容。更新后应核对来源能力、采集日志和真实运行样例。

## 结果口径

- `success / partial / failed` 来自最终回复的明确陈述或保守规则推断。
- `unknown`：完成回执缺失、语义不足或无法确认。
- `running`：最近六小时内已开始但未见完成回执，属于日志推断，非实时进程探测。
- 原文明确不代表再次验收业务系统；规则推断显示依据。
- `ACCEPTED / ARCHIVED / PENDING_REVIEW` 是查看状态，绝不当作业务成功。
- 历史时间窗口按启动时间统计，时区 Asia/Shanghai；运行时长含会话内等待与重试。
- 疑似逾期以调度器下次运行时间已过两小时为线索，仅在采集新鲜时提醒，不据此断言漏跑。

## 公开数据边界

只发布白名单字段中的名称、时间、状态、摘要和判断依据。工具调用、原始日志、提示词和原始数据库不进入仓库。摘要移除 URL、邮箱、IP、本地路径、常见令牌、飞书标识及长标识；发布前执行二次扫描。自动脱敏不是对任意自由文本的绝对保证，因此摘要保守截断。

`.local/` 是本机私有缓存、日志及发布回执，被 Git 忽略。禁止将它或完整 Codex 目录加入仓库。页面添加 `noindex` 只用于减少搜索收录，不是访问控制。公开页面的数据对访问者可见。

`codex://threads/...` 是电脑上的原任务入口，手机或另一台没有原任务的设备通常不能打开。

## 本地使用

需要 Python 3.9+、Git、可读取的本机 Codex 数据，以及有该仓库写权限的 GitHub 认证。本机使用已登录的 GitHub CLI 凭据助手通过 HTTPS 发布。

```sh
python3 scripts/collect.py
python3 scripts/publish.py --check-only
python3 -m http.server 4173 --bind 127.0.0.1 --directory docs
```

浏览器访问 `http://127.0.0.1:4173`。采集器不写源配置和数据库；缓存以文件大小及修改时间判断是否需要重新解析日志。

## 发布与自动更新

GitHub Pages 配置为 `main` 分支 `/docs`，由 GitHub 处理构建。仓库必须已有初始提交，Pages 必须先启用。私有仓库使用 Pages 需要支持的 GitHub 套餐。

手动完整同步：

```sh
python3 scripts/publish.py
```

发布器验证干净工作区，fetch/fast-forward，采集到本地暂存目录，校验后只提交 `docs/data/`，推送并重新 fetch 验证远端一致。并发执行通过文件锁互斥。网络失败后保留本地数据提交，下次尝试补推；不会 force push、自动解决分叉或提交其他人的修改。

代码更新通过 fast-forward 拉取后，下一次采集才运行新代码。本机 SSH 22 与 443 端口都出现过握手超时，因此仅此仓库的 origin 使用 HTTPS，由 `gh auth git-credential` 提供已有登录凭据；令牌不会写进远端 URL、代码或页面，不修改全局 SSH 配置。其他机器也可使用稳定的 SSH 连接。

安装当前用户的 launchd 任务（每半小时，登录后生效）：

```sh
python3 scripts/install_launchd.py
```

安装器会将当前终端中无账号密码的本机回环代理设置带入 launchd；macOS 后台任务不会自动继承终端代理。代理必须在后台运行时可用。代理地址变化后重新执行安装器即可。

查看状态：

```sh
launchctl print gui/$(id -u)/com.wellsleep.codex-task-dashboard
cat .local/publish-status.json
tail -n 30 .local/launchd.stderr.log
```

停止自动更新：

```sh
launchctl bootout gui/$(id -u)/com.wellsleep.codex-task-dashboard
```

从 `~/Library/LaunchAgents/` 移除该任务对应 plist 可取消下次登录加载。该任务独立于 Codex 的定时任务列表。

## 测试

```sh
python3 -m unittest discover -s tests -v
python3 scripts/publish.py --check-only
git diff --check
```

测试覆盖动态新任务、ID 重用、重复发现去重、原对话标记、字段变化、来源缺失、源数据只读、状态分类、脱敏和每小时心跳。页面交互与手机布局另用浏览器验收。
