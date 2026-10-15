# Pi 接入：能力与启动

入口 [get_adapters](__init__.py) 提供父端和子端。先读 [公共准备](../README.md#启动责任与公共准备)，再按下列步骤启动。以下说明针对当前 worktree，未安装到全局环境。

## 能力与边界

| 能力 | Pi 原生 | 当前适配 |
| --- | --- | --- |
| 父端接收子端汇报/请求 | 扩展注入 | session HTTP 主动推送；父端保持交互模式 |
| 子端接收父端问题 | RPC prompt | 控制器 HTTP → RPC stdin；不写 inbox |
| steer | RPC `steer` | 控制器 `send --mode steer` |
| queue | RPC `follow_up` | 控制器 `send --mode follow-up` |
| interrupt | RPC `abort` | 当前控制器 `abort` **先 clear_queue 再 abort**，不是保留队列的原生 abort |
| 状态、退出 | get_state、关闭 stdin | 控制器 `status`、`stop` |

`steer` 在当前助手轮次工具执行完、下一次模型调用前介入；`follow_up` 在没有工具调用与 steering 后处理。直接 steer/follow_up 不承诺启动空闲会话；`send --mode prompt` 使用 `streamingBehavior:steer`，空闲时启动，忙碌时加入 steering。无 RPC descriptor 时，子端适配器按绑定连接交互 Pi 的 session HTTP 接口；该接口也不存在时明确失败，不写 inbox。统一 interrupt 的保留/清空队列选项尚未实现。

## 启动专用子端

先生成 Pi 子会话 UUID，将同一个值填入绑定的 `child.session_id`；`child.framework=pi`。父端服务就绪后，将以下长驻命令交给运行器，cwd 为 REPO：

```bash
PI_BIN=/absolute/path/to/pi
python3 -m agent_link.hosts.pi.rpc --root "$ROOT" --binding "$BINDING" serve \
  --pi-bin "$PI_BIN" --cwd "$WORK" \
  --credentials "$ROOT/child-credentials.json" \
  --extension "$REPO/extensions/pi/agent-link.ts" \
  --provider deepseek --model deepseek-flash
```

provider/model 必须是本机 Pi 可用的标识；这里沿用本机 DeepSeek 配置。凭据由 Pi 自身读取，Agent Link 不请求模型 API。控制器显式加载此扩展，并禁用其他扩展自动发现、skills 和 context files；需要项目上下文时写入任务文本，勿假设它会自动加载。

就绪条件：stdout `ready:true`，且控制器已用 `get_state` 验证返回 sessionId 与 binding 一致。`$ROOT/pi-rpc/$BINDING.json` 是私有连接描述，不是收件箱，不打印其中 token。

```bash
python3 -m agent_link.hosts.pi.rpc --root "$ROOT" --binding "$BINDING" \
  send --mode prompt --text-file /absolute/path/to/task.md
python3 -m agent_link.hosts.pi.rpc --root "$ROOT" --binding "$BINDING" status
python3 -m agent_link.hosts.pi.rpc --root "$ROOT" --binding "$BINDING" \
  send --mode steer --text '补充当前任务的约束'
python3 -m agent_link.hosts.pi.rpc --root "$ROOT" --binding "$BINDING" \
  send --mode follow-up --text '当前工作结束后检查文档'
```

prompt 接受不是完成；用 `answer_parent` 的持久化答复证明模型处理了父端问题。运行结束后控制器保持存活，可接下一条消息。

## 交互父端与已有进程

Pi 父会话启动时加载 `--extension "$REPO/extensions/pi/agent-link.ts"`。扩展按真实 session id 注册本地 HTTP 接口，服务用 `--framework pi` 并按 binding 直接投递；没有父端文件轮询。启动、认证、残留处理见 [主动通知](../../../docs/SESSION-PUSH.md)。父端无需 RPC。
已有 Agent Link 控制器可通过同 root/binding 的 CLI 连接。不能接管任意已经启动的 Pi 终端进程的 stdin/stdout；这类进程需另起专用 RPC 子端，不能声称已 attach。

## 停止与验证范围

```bash
python3 -m agent_link.hosts.pi.rpc --root "$ROOT" --binding "$BINDING" abort
python3 -m agent_link.hosts.pi.rpc --root "$ROOT" --binding "$BINDING" stop
```

abort 清队列并中断当前操作，stop 才关闭控制器和专用 Pi。随后停止父端通信服务，核对进程退出和 descriptor 清理。超时结果可能已被接受，不自动重发。

RPC 已完成独立模拟测试，以及真实 DeepSeek 的空闲唤醒、report_parent 和 answer_parent 闭环；无 inbox 文件。交互 Pi 与 OMP 的 HTTP 推送临时探针也已获得模型答复，但正式父端已改为 HTTP 推送。正式父端推送已验证空闲通知、工具忙碌时 followUp 与 ask/reply；中断竞争、会话切换与重连的模型端到端状态为 unknown。细节与测试入口见 [PI-RPC](../../../docs/PI-RPC.md)。原生命令依据 [Pi RPC 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md)。
