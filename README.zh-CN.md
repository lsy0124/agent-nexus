# Agent Nexus

[English](README.md) · [贡献规范](CONTRIBUTING.md) · [安全边界](SECURITY.md)

Windows 本地多模型协作工作台，使用 React / TypeScript、FastAPI、SQLite、
WebSocket 和 Git Worktree。当前版本 0.4.0，是本地工程候选版本；尚未在此声称
公开发布、外部用户数量或当前服务商可用性。

## 已实现的范围

- 校验结构化任务规划、依赖和互斥输出目录，按批准范围进行有界并行。
- 分别记录 Agent、运行时、模型、供应商，以及声明能力和真实验证状态。
- 采集服务商实际报告的 Token；缺失保持缺失，估算费用不冒充账单或余额。
- 按次批准付费调用，保留失败与中断记录，对结果不确定的请求暂停待审。
- 校验文本文件方案后写入生成的 Worktree，支持人工交接、差异审核及单独合并批准。

模型 API 不自动等于完整编程 Agent。当前文件方案协议不授予模型通用 Shell、
自行安装依赖或自动合并权限。Worktree 也不是操作系统沙箱。
详见 [架构与边界](docs/ARCHITECTURE.md)。

## 安装和启动

需要 Windows 10/11、Python 3.12、Node.js 24、Git 和 PowerShell。在仓库根目录执行：

```powershell
.\scripts\setup.ps1
# 仅浏览本地功能，禁用真实模型适配器。
$env:NEXUS_AGENTS = ','
$env:NEXUS_KEY_VAULT = 'memory'
.\scripts\start.ps1
```

访问 http://127.0.0.1:8765 ，Ctrl+C 停止前台服务。英文 README 解释了逗号禁用值：
旧版 PowerShell 对空字符串环境变量的处理不同，显式逗号避免意外回到默认全部适配器。
安装依赖需要联网；普通界面与离线测试不需要模型账号或付费。

后台启动可用 `scripts/launch.ps1`，停止用 `scripts/stop.ps1`。如果进程查询受限，
使用前台命令。不要为本项目改变系统执行策略或以管理员启动。

`.env.example` 仅供参考，不自动读取。启用执行端、配置密钥与批准调用的方法见
[配置与使用](docs/USAGE.md)。源码 ZIP 若无 Git 基线，需要在审查源码后用自己的
Git 身份初始化和提交；Git clone 已有基线。无须配置远程或上传代码。

## 测试和发布准备

```powershell
.\scripts\test.ps1
.\.venv\Scripts\python.exe scripts\browser_test.py
.\.venv\Scripts\python.exe scripts\release_source.py audit
```

浏览器验收使用独立仓库、数据库及已有 Chromium/Edge，不调用真实模型。
可用 `NEXUS_BROWSER_PATH` 指定浏览器路径。离线测试不是当前账号和供应商可用证明。
`smoke.py` 仅检查运行实例的健康、状态和页面，不写任务、不创建 Worktree。

发布前按 [清单](docs/RELEASE.md)导出经过白名单与扫描的干净候选版本。
不要直接上传整个开发目录或历史；`work/`、历史 `outputs/`、私有任务、密文和日志
均不应进入公开仓库。保留本地原件，不为发布删除现有工作。

项目自有源码采用 MIT；第三方依赖分别遵守 [原许可](THIRD_PARTY_NOTICES.md)。

[真实程序截图](docs/SCREENSHOTS.md)来自隔离夹具验收，模型执行端连接数为零，不代表真实模型任务成功。
