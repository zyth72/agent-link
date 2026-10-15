# 框架适配器入口规范

父/子是绑定中的角色。每个框架在 `agent_link/hosts/<framework>/` 下维护适配，统一由包入口 `__init__.py:get_adapters()` 返回 `HostAdapters(parent=..., child=...)`。不支持的一侧必须为 `None`，不得返回空实现冒充支持。

## 目录与类型

```text
agent_link/hosts/
  base.py          # ParentAdapter、ChildAdapter 抽象基类与 HostAdapters
  codex/
    __init__.py    # get_adapters
    parent.py      # 交互式 Codex 父端，通过桌面桥投递
  pi/
    __init__.py    # get_adapters
    parent.py      # Pi 交互父端的现有扩展桥适配
    child.py       # Pi RPC 子端投递适配
    rpc.py         # 子进程 RPC 控制器
  omp/
    __init__.py    # get_adapters
    parent.py
    child.py       # 已有 inbox 子端适配
  opencode/
    __init__.py    # get_adapters
    parent.py
    child.py       # 已有 inbox 子端适配
```

TypeScript 扩展继续在 `extensions/<framework>/`，它们是加载到真实框架进程中的接入代码；Python 目录是核心可调用的宿主侧适配。不要为凑目录复制 TS 扩展或共享实现。

`ParentAdapter` 与 `ChildAdapter` 使用 Python ABC，分别要求 `capabilities() -> dict`、`notify(root, binding, message) -> str` 和 `capabilities() -> dict`、`deliver(root, binding, message) -> str`。返回文本为审计详情，异常表示投递失败或未知。能力对象必须区分传输、运行中投递和未验证能力；返回实例只表示该角色实现存在，不表示当前框架进程已经连接。

`HostAdapters` 为具名数据结构，包含两个可空字段。框架入口必须可在没有模型、进程和凭据时导入，不得在导入或 `get_adapters()` 时启动进程、连接网络或发送消息。`load_adapters(framework)` 仅加载已登记框架；不支持角色由调用方明确报错。

## 角色与传输

- 父端：用户的交互式开发会话。适配器通过插件、扩展或宿主接口投递通知；不要求父端进入 RPC 模式。
- 子端：本次任务的专用会话。Pi 用 RPC 控制器直接写 stdin、阻塞读取 stdout 事件，不依赖定时扫描 inbox；OMP/OpenCode 保留已有原生扩展接入，未来按能力替换传输。
- 通信服务和 SQLite 请求状态属于核心，不移入某个框架目录；后台运行器仍独立管理运行与取消。
- `host_accepted` 仅表示传输接受，不等于模型已处理。`reply/answer` 与请求状态仍沿用核心规范。

## 路径迁移（不保留旧路径）

本次为显式破坏性重构：删除旧单文件驱动与转发模块，不提供旧类名重导出、不添加deprecated别名或薄兼容文件。源码、测试、skill、指南和配置示例全部使用新入口。

父端适配统一由 `get_adapters().parent` 获取，子端由 `.child` 获取。服务支持 `--framework` 按框架选择；自定义驱动若保留 `--driver MODULE:OBJECT` 则仅是可扩展入口，不用于维持旧内置路径。旧 runtime 绑定与全局安装不自动迁移，新功能使用新worktree里的新绑定测试。

Codex 父端 CLI 放在 `agent_link.hosts.codex.cli`；Pi RPC CLI 放在 `agent_link.hosts.pi.rpc`。旧 `agent_link.hosts.codex.cli`、`agent_link.adapter`、`hosts/omp_notifier.py` 不留转发。共享渲染器和文件桥移到中立 shared 模块，其他框架不得依赖Codex/OMP目录实现共享基础功能。TypeScript扩展如需相应整理，只保留一个权威实现，不复制旧入口。

Codex 子端未实现时 `child=None`。生命周期能力（start/stop/status/steer）不是所有 ChildAdapter 的强制方法，Pi RPC 控制器通过单独能力接口提供。入口对象存在不等于进程已连接，需单独检查runtime状态。

## 验证

验证入口无副作用、两侧基类继承、角色缺失明确报错、旧导入路径已移除，以及核心投递确实调用入口返回的对象。框架能力需要真实接入或模拟证据分别标注。已有公共接口与其他框架的未提交改动应保留。
