# 图灵完备助手（tc_companion）

给《Turing Complete》用的 N.E.K.O. 陪玩插件：读手册、读关卡测试、写 ISA/汇编、检查与部署电路存档、按进度验收。

## 反推到的事实

游戏是 **Nim 引擎 + Godot 启动器**（Steam appid 1444480）。`circuit.data` 是版本化二进制（当前玩家存档首字节 `0x0f`，旧关卡文件是 `0x0d`）；`spec.isa`、`new_program.asm`、手册、关卡测试都是文本。

| 位置 | 内容 |
|---|---|
| `%APPDATA%\Turing Complete\settings.txt` | 当前关卡、架构 |
| `%APPDATA%\Turing Complete\levels.txt` | 每关完成状态与分数 |
| `...\schematics\<level>\<name>\circuit.data` | 电路（二进制） |
| `...\schematics\<level>\<name>\spec.isa` / `new_program.asm` | 指令集定义 / 汇编程序 |
| `<game>\asset\manual\**\title.txt` + `doc.txt` | 内置手册 |
| `<game>\campaign\<level>\meta.txt` / `test.si` / `ui.txt` | 关卡定义与测试 |

## 入口

| 入口 | 作用 |
|---|---|
| `tc_status` | 当前关卡、架构、进度、电路数量 |
| `tc_docs` | `list` / `search` / `read` 手册；`level` 看关卡 meta/test/ui |
| `tc_code` | `list` / `read` / `write` / `validate` ISA 与汇编 |
| `tc_circuit` | `list` / `info` / `deploy` 电路存档 |
| `tc_accept` | `check` / `snapshot` / `test` |
| `tc_bridge` | 原生控制桥：`status` / `run` / `stop` / `reset` / `place` / `undo` / `redo` / `save` / `components`（改动类动作受 `allow_write` 约束） |
| `tc_solve` | `context` 读关卡/测试/代码；`apply` 写 ISA/汇编并校验（需退出游戏）；`verify` 游戏运行时经控制桥跑关卡并对比 `levels.txt` |

`validate` 会检查：指令名是否在 `spec.isa` 里、标签是否重复或未定义、所有指令位宽是否一致。

## 电路能力边界

`circuit.data` 是私有二进制格式，插件**编辑存档时不会凭空生成元件或新连线**。它能做的是：

- 读取并展示电路版本、大小、内嵌的端口/标签字符串；
- 把你已经保存过的某个关卡电路，整份复制到目标关卡（先备份）。

这是有意保守的选择：乱改二进制会直接毁掉存档，而且无法验证。

例外：如果装了原生控制桥，`tc_bridge action=place` 会在**运行中的游戏**里直接放元件（随后由游戏自己保存）。

## 原生控制桥（可选，默认不安装）

`tc_bridge` 和 `tc_solve verify` 通过游戏内 mod 的本地 TCP 口工作，默认 `127.0.0.1:47654`，端口可配置（见下）。安装要改动游戏目录，所以默认关闭，需要在配置里写 `install_bridge = true`（且 `allow_write = true`）后重启插件。安装只在游戏未运行时进行，会：

1. 把 `<game>\game_engine.dll` 改名为 `tc_game_engine.dll`（原版引擎，哈希校验过），再把捆绑的代理加载器复制成 `game_engine.dll`；未知版本的 DLL 一律拒绝覆盖；
2. 把 `local.tc-bridge.mod` 复制到 `<game>\mods\`；
3. 运行捆绑的 `tcmod-cli.exe <game> enable local.tc-bridge`。

卸载：退出游戏 → 删除 `<game>\game_engine.dll` → 把 `tc_game_engine.dll` 改回 `game_engine.dll` → 删除 `<game>\mods\local.tc-bridge.mod`。也可以在 Steam 里「验证游戏文件完整性」恢复原版引擎。

### 桥的通信配置

| 配置项 | 默认 | 作用 |
|---|---|---|
| `bridge_port` | `47654` | 插件连接的控制桥端口。捆绑的 mod 固定监听 47654，只有 mod 侧同步改过端口才需要改这里。 |
| `bridge_token` | `""`（不发送） | 非空时插件在每个请求里加 `token` 字段。捆绑的 mod 会忽略该字段，只有换用会校验令牌的 mod 时才有实际门禁。 |
| `bridge_max_bytes` | `2097152` | 应答字节上限（2 MiB）。超限立即断开并报错，防止失控应答耗尽内存。 |

## 游戏验证（`verify_game`）

`auto`（默认）= 检测到游戏安装才开；`on` / `off` 强制。`tc_status` 会报告安装目录、exe 是否存在、控制桥是否已装。开启时：

- `tc_code write` / `tc_solve apply` / `tc_circuit deploy` 写完回读，结果里带 `verified`；
- `tc_bridge action=save` 之后最多等 3 秒看当前关卡 `circuit.data` 的修改时间是否变化，结果里带 `verified`（存档名按 `levels.txt` 推断）；place/undo/redo/reset 只改游戏内存，没有文件可回读，原样返回控制桥的回复；
- `tc_solve action=verify`：先记下 `levels.txt` 里该关的完成/分数，经控制桥 `run` 跑指定周期，再轮询 `levels.txt`，返回 `passed` / `already_done` / `not_passed`。只在游戏已运行时进行，插件不会替你启动游戏。

## 使用前提

- 写代码/部署电路前先退出游戏（游戏打开关卡时会覆盖文件）；`tc_solve apply` 同样遵守 `allow_write` 和这条限制。
- 部署后重新进入关卡让游戏读取新存档。

## 自检

```powershell
python tc.py
```

## 更新记录（2026-11-13）

- 新增 `tc_install` 入口：`status` 只读检查 `game_engine.dll` 状态，`install` 装加载器，`restore` 换回原版引擎；`install`/`restore` 需要 `allow_write=true` 且显式 `confirm=true`。
- DLL 与 mod 改为临时文件 + 哈希校验 + 原子替换，失败时原文件保持原样；覆盖同名 mod 前自动备份。
- `tc_solve action=apply` 先校验 spec.isa 和汇编，有错误就不写盘（原来的顺序是先写后校验）。
- `level` 与 `schematic` 只接受单层目录名，带 `/`、`\`、`..` 或控制字符会被拒绝；拼出的路径必须仍在 `schematics`/`campaign` 目录内。
- 启动时若未开启 `install_bridge`，`tc_status` 会显示只读探测结果（引擎状态、有没有可用加载器、备份是否完好）。

## 更新记录（2026-11-14）

- 控制桥加固：端口（`bridge_port`）、令牌（`bridge_token`）、应答字节上限（`bridge_max_bytes`）改为可配置；超限应答立即断开报错，不再无上限累积。
- `tc_status` 新增 `bridge_port` 和 `bridge_token_set`，一眼看出连的是哪个端口、有没有配令牌。
