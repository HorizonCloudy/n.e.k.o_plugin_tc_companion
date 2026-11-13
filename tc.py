"""Turing Complete 存档层控制核心。

游戏（Nim 引擎，Steam appid 1444480）把状态放在：
  %APPDATA%\\Turing Complete\\settings.txt      当前关卡 / 架构
  %APPDATA%\\Turing Complete\\levels.txt        每关完成状态与分数
  %APPDATA%\\Turing Complete\\schematics\\<level>\\<name>\\circuit.data   电路（二进制）
                                                    spec.isa / new_program.asm（文本）
  <game>\\asset\\manual\\**\\title.txt,doc.txt   内置手册
  <game>\\campaign\\<level>\\meta.txt,test.si,ui.txt   关卡定义与测试

本模块只做：读文档、读写代码、检查/部署电路、读进度做验收。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path

GAME_EXE = "Turing Complete.exe"
# 没有 libraryfolders.vdf 时的兜底库根目录；有 vdf 就以 vdf 为准。
STEAM_ROOTS = (
    r"E:\Program Files (x86)\Steam",
    r"C:\Program Files (x86)\Steam",
    r"D:\SteamLibrary",
)
VDF_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')
NIM_LITERAL_RE = re.compile(r"`(.*)`", re.S)

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT_DEFAULT = 47654
BRIDGE_MAX_BYTES_DEFAULT = 2 * 1024 * 1024


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def default_user_dir() -> Path:
    roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    isolated = Path(roaming) / "Turing Complete Mods" / "profiles" / "default"
    if (isolated / "settings.txt").exists():
        return isolated
    return Path(roaming) / "Turing Complete"


def resolve_user_dir(cfg: dict | None = None) -> Path:
    raw = str((cfg or {}).get("user_dir") or "").strip()
    return Path(raw) if raw else default_user_dir()


def resolve_game_dir(cfg: dict | None = None) -> Path | None:
    raw = str((cfg or {}).get("game_dir") or "").strip()
    if raw:
        return Path(raw)
    for lib in steam_libraries():
        path = lib / "steamapps" / "common" / "Turing Complete"
        if (path / GAME_EXE).exists():
            return path
    return None


def vdf_library_paths(text: str) -> list[Path]:
    """libraryfolders.vdf 里每个库的 "path"（反斜杠是转义过的）。"""
    return [Path(raw.replace("\\\\", "\\")) for raw in VDF_PATH_RE.findall(text or "")]


def steam_libraries() -> list[Path]:
    """注册表 SteamPath + 兜底根目录，再展开各自 libraryfolders.vdf 里的库。"""
    roots = [Path(r) for r in STEAM_ROOTS]
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            roots.insert(0, Path(winreg.QueryValueEx(key, "SteamPath")[0]))
    except (ImportError, OSError):
        pass
    libs: list[Path] = []
    for root in roots:
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for lib in [root, *vdf_library_paths(text)]:
            if lib not in libs:
                libs.append(lib)
    return libs


def verify_enabled(cfg: dict | None, game: Path | None) -> bool:
    """verify_game：auto = 检测到游戏安装才开；on / off 强制开关。"""
    mode = str((cfg or {}).get("verify_game", "auto")).strip().lower()
    if mode == "auto":
        return bool(game and (Path(game) / GAME_EXE).exists())
    return mode in ("on", "true")


# ------------------------------------------------------------------ settings


def read_settings(user: Path) -> dict:
    path = Path(user) / "settings.txt"
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if " = " in line:
            key, value = line.split(" = ", 1)
            out[key.strip()] = value.strip()
    return out


def read_levels(user: Path) -> list[dict]:
    path = Path(user) / "levels.txt"
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        row = next(csv.reader([line]), [])
        if len(row) < 3:
            continue
        out.append(
            {
                "level": row[0],
                "done": row[1].strip().lower() == "true",
                "schematic": row[2],
                "score": (row[3].rstrip("|") if len(row) > 3 else ""),
            }
        )
    return out


def level_progress(levels: list[dict], level: str) -> dict | None:
    for row in levels:
        if row["level"] == level:
            return row
    return None


# ---------------------------------------------------------------------- docs


def nim_text(raw: str) -> str:
    """`(31337_123, \\`text\\`)` -> `text`；不是该形式就原样返回。"""
    match = NIM_LITERAL_RE.search(raw or "")
    return match.group(1) if match else (raw or "").strip()


def manual_dir(game: Path) -> Path:
    return Path(game) / "asset" / "manual"


def list_docs(game: Path) -> list[dict]:
    root = manual_dir(game)
    if not root.exists():
        return []
    docs = []
    for title_file in sorted(root.rglob("title.txt")):
        folder = title_file.parent
        doc_file = folder / "doc.txt"
        docs.append(
            {
                "path": folder.relative_to(root).as_posix(),
                "title": nim_text(title_file.read_text(encoding="utf-8", errors="replace")),
                "doc": nim_text(doc_file.read_text(encoding="utf-8", errors="replace")) if doc_file.exists() else "",
            }
        )
    return docs


def search_docs(docs: list[dict], query: str, limit: int = 20) -> list[dict]:
    needle = (query or "").strip().lower()
    if not needle:
        return docs[: max(1, min(limit, 100))]
    hits = [d for d in docs if needle in d["title"].lower() or needle in d["doc"].lower()]
    return hits[: max(1, min(limit, 50))]


def find_doc(docs: list[dict], key: str) -> dict | None:
    wanted = (key or "").strip().lower()
    for doc in docs:
        if doc["path"].lower() == wanted or doc["title"].lower() == wanted:
            return doc
    for doc in docs:
        if wanted and (wanted in doc["path"].lower() or wanted in doc["title"].lower()):
            return doc
    return None


# --------------------------------------------------------------- level files


BAD_PATH_CHARS = set('/\\:*?"<>|')


def safe_component(value: str, kind: str) -> str:
    """关卡 id 和电路图名只能是一层目录名，不允许带路径。"""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"{kind} 不能为空")
    if raw in (".", "..") or any(ch in BAD_PATH_CHARS for ch in raw):
        raise ValueError(f"{kind} 非法：{value!r}（不能带路径分隔符或 ..）")
    if any(ord(ch) < 32 for ch in raw) or len(raw) > 80:
        raise ValueError(f"{kind} 非法：{value!r}（控制字符或过长）")
    return raw


def under(root: Path, target: Path, kind: str) -> Path:
    """确认 target 仍在 root 里面，防止拼出预期目录之外的路径。"""
    base = Path(root).resolve()
    resolved = Path(target).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"{kind} 越出预期目录：{resolved}")
    return resolved


def campaign_dir(game: Path, level: str) -> Path:
    root = Path(game) / "campaign"
    folder = root / safe_component(level, "level")
    return under(root, folder, "level")


def level_info(game: Path, level: str) -> dict:
    folder = campaign_dir(game, level)
    if not folder.exists():
        raise FileNotFoundError(f"找不到关卡定义：{folder}")
    out = {"level": level, "path": str(folder)}
    for name in ("meta.txt", "test.si", "ui.txt", "label.txt", "hint_0.txt"):
        path = folder / name
        if path.exists():
            out[name] = path.read_text(encoding="utf-8", errors="replace")
    meta = out.get("meta.txt") or ""
    title_match = re.search(r"^title\s*=\s*(.+)$", meta, re.M)
    kind_match = re.search(r"^kind\s*=\s*(\w+)", meta, re.M)
    out["title"] = nim_text(title_match.group(1)) if title_match else level
    out["kind"] = kind_match.group(1) if kind_match else ""
    out["has_hint_solution"] = (folder / "hint_solution.data").exists()
    return out


# ----------------------------------------------------------------- circuits


def schematic_dir(user: Path, level: str, name: str = "Default") -> Path:
    root = Path(user) / "schematics"
    folder = root / safe_component(level, "level") / safe_component(name or "Default", "schematic")
    return under(root, folder, "schematic")


def list_schematics(user: Path) -> list[dict]:
    root = Path(user) / "schematics"
    if not root.exists():
        return []
    out = []
    for circuit in root.rglob("circuit.data"):
        folder = circuit.parent
        try:
            parts = folder.relative_to(root).parts
            level = parts[0] if parts else ""
            name = "/".join(parts[1:]) if len(parts) > 1 else "Default"
        except ValueError:
            level, name = "", folder.name
        out.append(
            {
                "level": level,
                "schematic": name,
                "path": str(circuit),
                "size": circuit.stat().st_size,
                "modified": datetime.fromtimestamp(circuit.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return sorted(out, key=lambda x: x["modified"], reverse=True)


def circuit_info(path: Path) -> dict:
    data = Path(path).read_bytes()
    strings = []
    for match in re.finditer(rb"[ -~]{3,}", data):
        text = match.group().decode("ascii").strip()
        if len(text) < 3:
            continue
        if text not in strings:
            strings.append(text)
    return {
        "path": str(path),
        "size": len(data),
        "version": data[0] if data else None,
        "strings": strings[:60],
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def deploy_circuit(src: Path, dst_dir: Path, verify: bool = False) -> dict:
    src = Path(src)
    if src.name != "circuit.data" or not src.exists():
        raise FileNotFoundError(f"源电路不存在：{src}")
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "circuit.data"
    backup = None
    if dst.exists():
        backup = dst.with_name(f"{dst.name}.tc-bak-{now_stamp()}")
        shutil.copy2(dst, backup)
    data = src.read_bytes()
    tmp = dst.with_name(f".circuit.data.tmp-{now_stamp()}")
    tmp.write_bytes(data)
    os.replace(tmp, dst)
    out = {"source": str(src), "target": str(dst), "backup": str(backup) if backup else None}
    if verify:
        out["verified"] = dst.read_bytes() == data
    return out


# --------------------------------------------------------------------- code


def code_path(sdir: Path, name: str) -> Path:
    raw = (name or "").strip()
    if not raw.endswith((".isa", ".asm")):
        raise ValueError("只允许读写的代码文件：spec.isa / new_program.asm")
    if "/" in raw or "\\" in raw:
        raise ValueError("文件名不能带路径")
    return Path(sdir) / raw


def read_code(sdir: Path, name: str) -> str:
    return code_path(sdir, name).read_text(encoding="utf-8", errors="replace")


def write_code(sdir: Path, name: str, code: str, verify: bool = False) -> dict:
    path = code_path(sdir, name)
    if len(code.encode("utf-8")) > 500_000:
        raise ValueError("文件超过 500KB，拒绝写入")
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{now_stamp()}")
        shutil.copy2(path, backup)
    text = (code or "").replace("\r\n", "\n").replace("\r", "\n")
    tmp = path.with_name(f".{path.name}.tmp-{now_stamp()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    out = {"file": path.name, "bytes": len(text.encode("utf-8")), "backup": str(backup) if backup else None}
    if verify:
        out["verified"] = path.read_text(encoding="utf-8") == text
    return out


def parse_isa(text: str) -> dict:
    """返回 instructions / patterns / errors。指令区按 (签名, 位模式) 成对读取。"""
    section = ""
    seq: list[str] = []
    settings = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "settings" and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip('"')
        elif section == "instructions":
            seq.append(line)

    instructions, patterns, errors = [], {}, []
    for i in range(0, len(seq) - 1, 2):
        sig, pattern = seq[i], seq[i + 1]
        name = sig.split()[0] if sig.split() else ""
        if not name:
            continue
        if not re.fullmatch(r"[01a-zA-Z]+", pattern):
            errors.append(f"指令 {name} 的位模式不像二进制：{pattern!r}")
            continue
        instructions.append(name)
        patterns[name] = pattern
    if len(seq) % 2:
        errors.append("instructions 区有落单的行（应为 签名/位模式 成对）")
    widths = {len(p) for p in patterns.values()}
    if len(widths) > 1:
        errors.append(f"指令位宽不一致：{sorted(widths)}")
    if len(instructions) != len(set(instructions)):
        errors.append("有重复的指令名")
    return {"settings": settings, "instructions": instructions, "patterns": patterns, "errors": errors}


def validate_asm(text: str, isa: dict) -> dict:
    known = set(isa.get("instructions") or [])
    errors, warnings = [], []
    labels, refs = set(), []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split(";", 1)[0].rstrip()
        if not line.strip():
            continue
        if line != line.lstrip("\t") and line[:1] == " ":
            warnings.append(f"第 {lineno} 行空格缩进（游戏示例用 Tab）")
        stripped = line.strip()
        if ":" in stripped:
            label = stripped.split(":", 1)[0].strip()
            if label and re.fullmatch(r"\w+", label):
                if label in labels:
                    errors.append(f"第 {lineno} 行标签重复：{label}")
                labels.add(label)
                stripped = stripped.split(":", 1)[1].strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        op = parts[0]
        if known and op not in known:
            errors.append(f"第 {lineno} 行未知指令：{op}")
        if op == "imm" and len(parts) > 1:
            operand = parts[1].split(None, 1)[0].strip(",")
            if operand and not re.fullmatch(r"[-+]?\d+", operand):
                refs.append((lineno, operand))
    for lineno, ref in refs:
        if ref not in labels:
            warnings.append(f"第 {lineno} 行引用了未定义标签：{ref}")
    return {"errors": errors, "warnings": warnings, "instructions_known": len(known)}


# ------------------------------------------------------------------ process


def game_pid(exe: str = GAME_EXE) -> int | None:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for row in csv.reader(io.StringIO(out)):
        if row and row[0].lower() == exe.lower():
            try:
                return int(row[1])
            except (IndexError, ValueError):
                return None
    return None


def is_game_running(exe: str = GAME_EXE) -> bool:
    return game_pid(exe) is not None


# ---------------------------------------------------------------- self-check


class BridgeError(ValueError):
    """控制桥配置无效、应答超限或应答不是 JSON。"""


def bridge_port(cfg: dict | None) -> int:
    """配置项 bridge_port；缺省 47654，必须是 1-65535。"""
    raw = (cfg or {}).get("bridge_port", BRIDGE_PORT_DEFAULT)
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise BridgeError(f"bridge_port 不是有效端口：{raw!r}") from exc
    if not 1 <= port <= 65535:
        raise BridgeError(f"bridge_port 超出 1-65535：{port}")
    return port


def bridge_max_bytes(cfg: dict | None) -> int:
    """配置项 bridge_max_bytes；应答字节上限，最小 1024。"""
    raw = (cfg or {}).get("bridge_max_bytes", BRIDGE_MAX_BYTES_DEFAULT)
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise BridgeError(f"bridge_max_bytes 不是有效字节数：{raw!r}") from exc
    if limit < 1024:
        raise BridgeError(f"bridge_max_bytes 太小（至少 1024）：{limit}")
    return limit


def bridge_request(
    payload: dict,
    *,
    port: int = BRIDGE_PORT_DEFAULT,
    timeout: float = 8.0,
    max_bytes: int = BRIDGE_MAX_BYTES_DEFAULT,
    token: str = "",
) -> dict:
    """往 127.0.0.1:port 发一行 JSON，读一行 JSON 应答。

    token 非空时给请求加 token 字段；应答超过 max_bytes 立即断开报错。
    连不上或超时按 OSError 抛出，由调用方转成插件错误。
    """
    msg = dict(payload)
    if token:
        msg["token"] = token
    with socket.create_connection((BRIDGE_HOST, port), timeout=timeout) as sock:
        sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        parts = []
        total = 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise BridgeError(f"控制桥应答超过 {max_bytes} 字节上限，已断开")
            parts.append(chunk)
            if chunk.endswith(b"\n"):
                break
    data = b"".join(parts)
    if not data:
        raise BridgeError("控制桥返回空应答")
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise BridgeError(f"控制桥返回了非 JSON：{data[:200]!r}") from exc


def _selftest() -> None:
    import tempfile
    import threading

    assert nim_text("(31337_123, `AND Gate`)") == "AND Gate"

    levels_text = '"and_gate",true,"Default",\n"maze",false,"Overture"\n'
    rows = []
    for line in levels_text.splitlines():
        row = next(csv.reader([line]), [])
        if len(row) >= 3:
            rows.append({"level": row[0], "done": row[1] == "true", "schematic": row[2]})
    assert rows[0]["done"] and not rows[1]["done"]

    isa_text = (
        "[settings]\nname = \"T\"\n\n[instructions]\n"
        "mov %a, %b\n10aaabbb\nnand\n01000000\nimm %a(label)\n00aaaaaa\n"
    )
    isa = parse_isa(isa_text)
    assert isa["instructions"] == ["mov", "nand", "imm"] and not isa["errors"]
    asm = "loop:\n\tmov r1, in\n\timm go\n\tjmp\n"
    res = validate_asm(asm, isa)
    assert any("未知指令" in e and "jmp" in e for e in res["errors"])
    assert any("未定义标签" in w and "go" in w for w in res["warnings"])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        user = root / "user"
        game = root / "game"
        src = user / "schematics" / "and_gate" / "Default"
        dst = user / "schematics" / "xor_gate" / "Default"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        (src / "circuit.data").write_bytes(b"\x0f\xc0\x04\x20Input\x00Out")
        (dst / "circuit.data").write_bytes(b"old")
        (src / "spec.isa").write_text(isa_text, encoding="utf-8")
        (dst / "new_program.asm").write_text(asm, encoding="utf-8")

        info = circuit_info(src / "circuit.data")
        assert info["version"] == 15 and "Input" in info["strings"]
        assert write_code(src, "spec.isa", isa_text, verify=True)["verified"]
        assert read_code(src, "spec.isa") == isa_text
        moved = deploy_circuit(src / "circuit.data", dst, verify=True)
        assert moved["backup"] and moved["verified"] and (dst / "circuit.data").read_bytes().startswith(b"\x0f")
        assert list_schematics(user)

        manual = manual_dir(game) / "Circuits" / "Delay"
        manual.mkdir(parents=True)
        (manual / "title.txt").write_text("(31337_1, `Delay`)", encoding="utf-8")
        (manual / "doc.txt").write_text("(31337_2, `All components have delay.`)", encoding="utf-8")
        docs = list_docs(game)
        assert docs[0]["title"] == "Delay" and "delay" in docs[0]["doc"].lower()
        assert find_doc(docs, "delay")["path"] == "Circuits/Delay"

        vdf = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"E:\\\\Steam"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"F:\\\\Lib"\n\t}\n}\n'
        assert vdf_library_paths(vdf) == [Path(r"E:\Steam"), Path(r"F:\Lib")]
        (game / GAME_EXE).write_bytes(b"")
        assert verify_enabled({}, game) and verify_enabled({"verify_game": "on"}, None)
        assert not verify_enabled({"verify_game": "off"}, game) and not verify_enabled({}, root / "nope")
    with tempfile.TemporaryDirectory() as gate:
        base = Path(gate)
        (base / "schematics").mkdir()
        assert schematic_dir(base, "Basic_logic", "Default").parts[-2:] == ("Basic_logic", "Default")
        for bad in ("..", "../x", "a/b", "a\\b", "", "x" * 90):
            try:
                schematic_dir(base, bad, "Default")
                raise AssertionError(f"level {bad!r} 没被拒绝")
            except ValueError:
                pass
        try:
            schematic_dir(base, "ok", "..\\..\\evil")
            raise AssertionError("schematic 越界没被拒绝")
        except ValueError:
            pass
        (base / "campaign").mkdir()
        for bad in ("..\\\\windows", ".."):
            try:
                campaign_dir(base, bad)
                raise AssertionError(f"campaign level {bad!r} 没被拒绝")
            except ValueError:
                pass
        assert bridge_port({}) == BRIDGE_PORT_DEFAULT and bridge_port({"bridge_port": "50000"}) == 50000
        for bad_cfg in ({"bridge_port": "abc"}, {"bridge_port": 0}, {"bridge_port": 65536}):
            try:
                bridge_port(bad_cfg)
                raise AssertionError(f"{bad_cfg} 的 bridge_port 没被拒绝")
            except BridgeError:
                pass
        assert bridge_max_bytes({}) == BRIDGE_MAX_BYTES_DEFAULT
        assert bridge_max_bytes({"bridge_max_bytes": 4096}) == 4096
        try:
            bridge_max_bytes({"bridge_max_bytes": 512})
            raise AssertionError("过小的 bridge_max_bytes 没被拒绝")
        except BridgeError:
            pass

        seen: list[dict] = []

        def serve(response: bytes) -> int:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            srv_port = srv.getsockname()[1]

            def run() -> None:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                finally:
                    srv.close()
                with conn:
                    buf = b""
                    while not buf.endswith(b"\n"):
                        try:
                            got = conn.recv(4096)
                        except OSError:
                            return
                        if not got:
                            break
                        buf += got
                    try:
                        seen.append(json.loads(buf.decode("utf-8")))
                    except json.JSONDecodeError:
                        pass
                    try:
                        conn.sendall(response)
                    except OSError:
                        pass

            threading.Thread(target=run, daemon=True).start()
            return srv_port

        port = serve(b'{"ok": true}\n')
        assert bridge_request({"cmd": "status"}, port=port, timeout=5.0, token="s3cret") == {"ok": True}
        assert seen[0]["cmd"] == "status" and seen[0]["token"] == "s3cret"
        port = serve(b'{"ok": 1}\n')
        assert bridge_request({"cmd": "ping"}, port=port, timeout=5.0) == {"ok": 1}
        assert "token" not in seen[1]
        port = serve(b'{"x": "' + b"A" * (3 * 1024 * 1024) + b'"}\n')
        try:
            bridge_request({"cmd": "components"}, port=port, timeout=5.0, max_bytes=1024 * 1024)
            raise AssertionError("超限应答没有被拦下")
        except BridgeError as exc:
            assert "超过" in str(exc)
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        try:
            bridge_request({"cmd": "ping"}, port=dead_port, timeout=1.0)
            raise AssertionError("连接失败没有被报告")
        except OSError:
            pass
        print("selftest ok")


if __name__ == "__main__":
    _selftest()
