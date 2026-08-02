"""
NonSteamCleaner - 彻底清理 Steam 中添加的非 Steam 游戏 (Decky Loader 后端)

功能:
  - 读取所有用户的 shortcuts.vdf，列出已添加的非 Steam 游戏
  - 提供预览将要删除的文件路径
  - 按选项删除:
        1) 本体
        2) 本体 + 存档 (compatdata 前缀，包含 Proton 前缀与存档)
        3) 本体 + 存档 + 着色器缓存
        4) 本体 + 着色器缓存
  - 同时清理 Steam 库中的快捷方式(shortcuts.vdf)以及对应的网格图片

注意:
  - 删除操作不可恢复，前端会有二次确认。
  - 修改 shortcuts.vdf 后需要重启 Steam 才能在库中消失。
  - "本体"会删除可执行文件及其 StartDir 所在的游戏目录。
  - "存档"通过删除 compatdata/<appid> 前缀实现，这同时会删除 Proton 前缀(注册表/配置)。
"""

import os
import re
import shutil
import struct
import zlib
import glob
import logging
from io import BytesIO
from typing import Any, Dict, List, Optional

# Decky 会注入 decky_plugin 模块（logger 等），但不导出 Plugin / ripple。
# 正确写法：定义 class Plugin，async 方法会自动暴露给前端 callPluginMethod。
try:
    import decky_plugin  # type: ignore

    logger = decky_plugin.logger
except Exception:  # noqa: BLE001
    logger = logging.getLogger("NonSteamCleaner")

STEAM_ROOTS = [
    os.path.expanduser("~/.steam/steam"),
    os.path.expanduser("~/.local/share/Steam"),
    "/home/deck/.steam/steam",
    "/home/deck/.local/share/Steam",
]

# 这些目录本身不能整目录删除（作为 StartDir 时只删 exe）
_PROTECT_START_DIRS = {
    "/home/deck",
    "/home/deck/Downloads",
    "/home/deck/Applications",
    "/home/deck/Desktop",
    "/home/deck/Games",
    "/home/deck/Emulation",
    "/usr",
    "/usr/bin",
    "/usr/local",
    "/usr/local/bin",
    "/opt",
    "/bin",
    "/sbin",
}

# 受保护目录，绝不允许删除
_PROTECT_BASE = [
    os.path.realpath(os.path.expanduser("~")),
    "/",
    "/home",
    "/home/deck",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/var",
    "/boot",
    "/proc",
    "/sys",
    "/dev",
    "/root",
]


# ---------------------------------------------------------------------------
# 二进制 VDF 解析 / 写入  (shortcuts.vdf 使用 Valve 二进制 KeyValues 格式)
#   0x00 = 子节点 (node)     0x08 = 当前节点结束
#   0x01 = string            0x02 = int32      0x03 = float
#   0x04 = pointer           0x05 = wstring    0x06 = color
#   0x07 = uint64
# ---------------------------------------------------------------------------
def _read_cstring(fp) -> str:
    buf = b""
    while True:
        c = fp.read(1)
        if not c or c == b"\x00":
            break
        buf += c
    return buf


def _read_node(fp) -> dict:
    node: Dict[str, Any] = {}
    while True:
        b = fp.read(1)
        if not b:
            break
        t = b[0]
        if t == 0x08:  # 节点结束
            break
        if t == 0x00:  # 子节点
            name = _read_cstring(fp).decode("utf-8", "replace")
            node[name] = _read_node(fp)
            continue
        name = _read_cstring(fp).decode("utf-8", "replace")
        if t == 0x01:  # string
            node[name] = _read_cstring(fp).decode("utf-8", "replace")
        elif t == 0x02:  # int32
            node[name] = struct.unpack("<i", fp.read(4))[0]
        elif t == 0x03:  # float
            node[name] = struct.unpack("<f", fp.read(4))[0]
        elif t == 0x04:  # pointer
            node[name] = struct.unpack("<i", fp.read(4))[0]
        elif t == 0x05:  # wstring
            slen = struct.unpack("<H", fp.read(2))[0]
            node[name] = fp.read(slen * 2).decode("utf-16-le", "replace")
        elif t == 0x06:  # color
            node[name] = struct.unpack("<I", fp.read(4))[0]
        elif t == 0x07:  # uint64
            node[name] = struct.unpack("<Q", fp.read(8))[0]
        else:
            # 未知类型: 尽力按字符串 key + 字符串 value 处理
            node[name] = _read_cstring(fp).decode("utf-8", "replace")
    return node


def _write_cstring(fp, s: str):
    fp.write(s.encode("utf-8", "replace"))
    fp.write(b"\x00")


def _write_int32(fp, key: str, value: int):
    fp.write(b"\x02")
    _write_cstring(fp, str(key))
    fp.write(struct.pack("<i", int(value)))


def _write_node(fp, node: dict):
    for key, value in node.items():
        if isinstance(value, dict):
            fp.write(b"\x00")  # 子节点
            _write_cstring(fp, str(key))
            _write_node(fp, value)
            fp.write(b"\x08")  # 子节点结束
        elif isinstance(value, bool):
            _write_int32(fp, str(key), 1 if value else 0)
        elif isinstance(value, int):
            # Steam shortcuts 的 appid 等字段期望 int32（可含负值表示高位 appid）。
            # 旧逻辑把 >0x7FFFFFFF 写成 uint64，Steam 有时无法正确丢掉条目。
            v = int(value)
            if -0x80000000 <= v <= 0x7FFFFFFF:
                _write_int32(fp, str(key), v)
            elif 0 <= v <= 0xFFFFFFFF:
                # 无符号 32 位 → 有符号 int32 位型
                as_signed = struct.unpack("<i", struct.pack("<I", v & 0xFFFFFFFF))[0]
                _write_int32(fp, str(key), as_signed)
            else:
                fp.write(b"\x07")
                _write_cstring(fp, str(key))
                fp.write(struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))
        elif isinstance(value, float):
            fp.write(b"\x03")
            _write_cstring(fp, str(key))
            fp.write(struct.pack("<f", value))
        else:  # 默认按字符串
            fp.write(b"\x01")
            _write_cstring(fp, str(key))
            _write_cstring(fp, str(value))


def write_vdf(path: str, root: dict):
    """原子写入 binary VDF，避免写到一半 Steam 读到半截文件。"""
    path = os.path.realpath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "wb") as fp:
            _write_node(fp, root)
            fp.write(b"\x08")  # 根节点结束
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:  # noqa: BLE001
                pass


def appid_to_steam_int32(appid: Any) -> int:
    """写入 shortcuts 时用的有符号 int32 appid。"""
    u = normalize_appid(appid)
    return struct.unpack("<i", struct.pack("<I", u & 0xFFFFFFFF))[0]


def remove_shortcuts_from_steam(
    *,
    userdata_id: str = "",
    key: str = "",
    appid: Any = 0,
    exe: str = "",
    name: str = "",
) -> Dict[str, Any]:
    """从所有（或指定）用户的 shortcuts.vdf 中移除匹配项。

    匹配优先级：key(+user) → appid → exe 路径 → 名称。
    解决：删了文件但库里还在 / key 对不上 / 多用户 / Steam 重写格式不一致。
    """
    root = find_steam_root()
    if not root:
        return {"removed": False, "removed_count": 0, "details": [], "message": "无 Steam 目录"}

    target_appid = normalize_appid(appid) if appid else 0
    target_exe = _normalize(exe) if exe else None
    target_name = (name or "").strip()
    target_key = str(key).strip() if key is not None and str(key).strip() != "" else ""
    prefer_sid = str(userdata_id or "").strip()

    ud_root = os.path.join(root, "userdata")
    if not os.path.isdir(ud_root):
        return {"removed": False, "removed_count": 0, "details": [], "message": "无 userdata"}

    sids = []
    if prefer_sid and os.path.isdir(os.path.join(ud_root, prefer_sid)):
        sids.append(prefer_sid)
    for sid in sorted(os.listdir(ud_root)):
        if sid not in sids:
            sids.append(sid)

    details: List[Dict[str, Any]] = []
    total_removed = 0

    for sid in sids:
        sc_path = os.path.join(ud_root, sid, "config", "shortcuts.vdf")
        if not os.path.isfile(sc_path):
            continue
        try:
            with open(sc_path, "rb") as fp:
                parsed = _read_node(fp)
        except Exception as e:  # noqa: BLE001
            logger.error("read shortcuts %s: %s", sc_path, e)
            details.append({"userdata_id": sid, "error": str(e)})
            continue

        shortcuts = parsed.get("shortcuts")
        if not isinstance(shortcuts, dict) or not shortcuts:
            continue

        to_del: List[str] = []
        for k, entry in list(shortcuts.items()):
            if not isinstance(entry, dict):
                continue
            hit = False
            reason = ""
            # 1) key 精确匹配（优先当前用户）
            if target_key and str(k) == target_key and (not prefer_sid or sid == prefer_sid):
                hit, reason = True, "key"
            # 2) appid
            if not hit and target_appid:
                try:
                    ea = normalize_appid(entry.get("appid"))
                except Exception:  # noqa: BLE001
                    ea = 0
                if ea and ea == target_appid:
                    hit, reason = True, "appid"
            # 3) exe 路径
            if not hit and target_exe:
                ee = _normalize(entry.get("Exe") or "")
                if ee and ee == target_exe:
                    hit, reason = True, "exe"
            # 4) 名称（仅当同时给了 appid 或 exe 时更安全——这里名称仅作补充，需 appid 也匹配失败时用）
            if not hit and target_name and target_exe:
                en = str(entry.get("AppName") or "").strip()
                if en and en == target_name:
                    hit, reason = True, "name+exe_context"

            if hit:
                to_del.append(str(k))
                details.append(
                    {
                        "userdata_id": sid,
                        "key": str(k),
                        "reason": reason,
                        "name": entry.get("AppName"),
                        "appid": normalize_appid(entry.get("appid")),
                        "exe": entry.get("Exe"),
                    }
                )

        if not to_del:
            continue

        # 备份后删除并重排 key 为 0..n-1（Steam 更稳）
        try:
            shutil.copy2(sc_path, sc_path + f".bak_nsc_rm_{int(__import__('time').time())}")
        except Exception:  # noqa: BLE001
            pass

        for k in to_del:
            if k in shortcuts:
                del shortcuts[k]
            # 有时 key 类型不一致
            for kk in list(shortcuts.keys()):
                if str(kk) == k:
                    del shortcuts[kk]

        # 重排
        items = []
        for k in sorted(shortcuts.keys(), key=lambda x: (len(str(x)), str(x))):
            items.append(shortcuts[k])
        new_map: Dict[str, Any] = {}
        for i, entry in enumerate(items):
            if isinstance(entry, dict) and "appid" in entry:
                try:
                    entry["appid"] = appid_to_steam_int32(entry.get("appid"))
                except Exception:  # noqa: BLE001
                    pass
            new_map[str(i)] = entry
        parsed["shortcuts"] = new_map

        try:
            write_vdf(sc_path, parsed)
            total_removed += len(to_del)
            # 校验
            with open(sc_path, "rb") as fp:
                check = _read_node(fp)
            still = check.get("shortcuts") or {}
            for d in details:
                if d.get("userdata_id") != sid:
                    continue
                # 确认 exe/appid 不再存在
                for e in still.values():
                    if not isinstance(e, dict):
                        continue
                    if target_exe and _normalize(e.get("Exe") or "") == target_exe:
                        d["verify"] = "still_present"
                        break
                    if target_appid and normalize_appid(e.get("appid")) == target_appid:
                        d["verify"] = "still_present"
                        break
                else:
                    d["verify"] = "gone"
        except Exception as e:  # noqa: BLE001
            logger.error("write shortcuts %s: %s", sc_path, e)
            details.append({"userdata_id": sid, "error": f"write: {e}"})

    return {
        "removed": total_removed > 0,
        "removed_count": total_removed,
        "details": details,
        "message": f"已从 shortcuts 移除 {total_removed} 条" if total_removed else "未在 shortcuts.vdf 中找到匹配项",
    }


# ---------------------------------------------------------------------------
# 路径辅助
# ---------------------------------------------------------------------------
def find_steam_root() -> Optional[str]:
    for r in STEAM_ROOTS:
        if os.path.isdir(r) and os.path.isdir(os.path.join(r, "steamapps")):
            return os.path.realpath(r)
    return None


def iter_steam_library_roots() -> List[str]:
    """主库 + libraryfolders.vdf 中的额外库（SD 卡等）。"""
    roots: List[str] = []
    seen = set()

    def _add(path: str):
        if not path:
            return
        rp = os.path.realpath(os.path.expanduser(path))
        if rp in seen:
            return
        if os.path.isdir(os.path.join(rp, "steamapps")):
            seen.add(rp)
            roots.append(rp)

    main = find_steam_root()
    if main:
        _add(main)
    for r in STEAM_ROOTS:
        _add(r)

    # 解析 libraryfolders.vdf
    candidates = []
    if main:
        candidates.append(os.path.join(main, "steamapps", "libraryfolders.vdf"))
        candidates.append(os.path.join(main, "config", "libraryfolders.vdf"))
    for lf in candidates:
        if not os.path.isfile(lf):
            continue
        try:
            text = open(lf, "r", encoding="utf-8", errors="replace").read()
        except Exception:  # noqa: BLE001
            continue
        # "path"\t\t"/some/path"  或 "path""/some/path"
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            _add(m.group(1))
    return roots


def compute_appid(exe: str, name: str) -> int:
    """Steam 非 Steam 快捷方式 appid：CRC32(exe+name) | 0x80000000。"""
    exe_n = (_normalize(exe) or str(exe or "")).replace("\\", "/")
    # Steam 写入 shortcuts 时 Exe 常带引号；CRC 一般用裸路径
    while len(exe_n) >= 2 and exe_n[0] == exe_n[-1] and exe_n[0] in "\"'":
        exe_n = exe_n[1:-1]
    key = f"{exe_n}{name or ''}"
    return (zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF) | 0x80000000


def normalize_appid(appid: Any) -> int:
    """统一 appid 为无符号 32 位，兼容 Steam UI 的有符号 int32。"""
    try:
        a = int(appid)
    except (TypeError, ValueError):
        return 0
    return a & 0xFFFFFFFF


def is_nonsteam_shortcut_appid(appid: Any) -> bool:
    """非 Steam 快捷方式 appid 最高位为 1（>= 0x80000000）。"""
    return normalize_appid(appid) >= 0x80000000


def _normalize(p: str) -> Optional[str]:
    """将 Steam 路径转为真实绝对路径。

    Steam shortcuts.vdf 常见格式：
      - 带引号: "/home/deck/game/game.exe"
      - Windows/Proton: Z:\\home\\deck\\...  或 Z:/home/deck/...
      - 相对路径较少见
    以前未去引号会导致 realpath 变成 /home/deck/"/home/deck/... 从而永远找不到文件。
    """
    if not p:
        return None
    p = str(p).strip()
    # 去掉包裹引号（可多层）
    while len(p) >= 2 and ((p[0] == p[-1] == '"') or (p[0] == p[-1] == "'")):
        p = p[1:-1].strip()
    p = p.replace("\\", "/")
    # 去掉残留的错误引号
    p = p.strip('"').strip("'")
    # Z:/home/deck/... 或 C:/...
    if len(p) >= 2 and p[1] == ":":
        p = p[2:]
    if not p:
        return None
    if not p.startswith("/"):
        # 非绝对路径：尽量拼到 home
        p = os.path.join(os.path.expanduser("~"), p)
    p = os.path.expanduser(p)
    try:
        return os.path.realpath(p)
    except Exception:  # noqa: BLE001
        return p


def _safe_to_delete(p: str) -> bool:
    if not p:
        return False
    try:
        rp = os.path.realpath(p)
    except Exception:  # noqa: BLE001
        return False
    if rp in _PROTECT_BASE:
        return False
    if rp in _PROTECT_START_DIRS:
        return False
    # 保护更浅的路径
    for protected in _PROTECT_START_DIRS:
        if rp == protected:
            return False
    root = find_steam_root()
    if root and rp == os.path.realpath(root):
        return False
    # 至少要有三层路径，避免误删过浅的目录
    parts = [x for x in rp.strip("/").split("/") if x]
    if len(parts) < 3:
        return False
    # 禁止删除整个 home 下仅两级的大目录之外已覆盖；再拦一层 /home/deck/xxx 太宽时只允许更深
    if len(parts) == 3 and parts[0] == "home" and parts[1] == "deck":
        # /home/deck/Downloads 等已在 _PROTECT_START_DIRS；其它如 /home/deck/foo 允许
        pass
    return True


def _appid_path_candidates(appid: Any) -> List[str]:
    """compatdata/shadercache/grid 可能用无符号或有符号 appid 目录名。"""
    u = normalize_appid(appid)
    if not u:
        return []
    names = {str(u)}
    # 有符号 int32 形式（少见，但兼容）
    if u >= 0x80000000:
        names.add(str(u - 0x100000000))
    names.add(str(int(u)))
    return list(names)


def _collect_prefix_dirs(appid: Any, kind: str) -> List[str]:
    """kind: 'compatdata' | 'shadercache'，跨所有 Steam 库查找。"""
    found: List[str] = []
    for lib_root in iter_steam_library_roots():
        base = os.path.join(lib_root, "steamapps", kind)
        if not os.path.isdir(base):
            continue
        for name in _appid_path_candidates(appid):
            path = os.path.join(base, name)
            if os.path.isdir(path):
                found.append(os.path.realpath(path))
        # 再扫一遍：有时目录名与 shortcuts 的 appid 不完全一致但低 32 位相同
        try:
            target = normalize_appid(appid)
            for entry in os.listdir(base):
                try:
                    if (int(entry) & 0xFFFFFFFF) == target:
                        path = os.path.realpath(os.path.join(base, entry))
                        if path not in found and os.path.isdir(path):
                            found.append(path)
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            pass
    return found


def _collect_grid_files(userdata_id: str, appid: Any) -> List[str]:
    root = find_steam_root()
    if not root or not userdata_id:
        return []
    grid_dir = os.path.join(root, "userdata", str(userdata_id), "config", "grid")
    if not os.path.isdir(grid_dir):
        return []
    out: List[str] = []
    for name in _appid_path_candidates(appid):
        # 2321292887.jpg / 2321292887p.jpg / 2321292887_hero.jpg 等
        for f in glob.glob(os.path.join(grid_dir, f"{name}*")):
            base = os.path.basename(f)
            if re.match(rf"^{re.escape(name)}(\.|p|_|$)", base):
                out.append(f)
    return out


# ---------------------------------------------------------------------------
# 扫描 / 添加非 Steam 游戏
# ---------------------------------------------------------------------------
_DEFAULT_SCAN_PATH = os.path.expanduser("~/Downloads")
_SETTINGS_FILE_CANDIDATES = [
    os.path.join(os.environ.get("DECKY_PLUGIN_SETTINGS_DIR", ""), "settings.json"),
    os.path.expanduser("~/homebrew/settings/NonSteamCleaner/settings.json"),
]

_SKIP_EXE_RE = re.compile(
    r"(unitycrashhandler|crashhandler|crashpad|uninstall|unins\d*|"
    r"vcredist|dxsetup|directx|redist|\.net|dotnet|setup\.exe|"
    r"notification_helper|steam\.exe|pythonw?\.exe|zsync|"
    r"payload|installer|crash_reporter|helper\.exe)",
    re.I,
)
_SKIP_DIR_NAMES = {
    "_commonredist",
    "redist",
    "directx",
    "support",
    "__macosx",
    "node_modules",
    ".git",
    ".svn",
    "__pycache__",
    "engine",
    "dotnet",
    "mono",
}


def _settings_path() -> str:
    decky_dir = (os.environ.get("DECKY_PLUGIN_SETTINGS_DIR") or "").strip()
    if decky_dir:
        return os.path.join(decky_dir, "settings.json")
    return os.path.expanduser("~/homebrew/settings/NonSteamCleaner/settings.json")


def load_settings() -> Dict[str, Any]:
    import json

    defaults: Dict[str, Any] = {
        "scan_path": _DEFAULT_SCAN_PATH,
        "max_depth": 5,
        "userdata_id": "",  # 空=自动选
        "auto_extract": True,  # 扫描时自动解压压缩包
        "extract_depth": 2,  # 压缩包嵌套解压层数
        "hidden_exes": [],  # 隐藏的启动项（归一化绝对路径）
    }
    path = _settings_path()
    try:
        if os.path.isfile(path):
            data = json.load(open(path, "r", encoding="utf-8"))
            if isinstance(data, dict):
                for k in list(defaults.keys()):
                    if k in data:
                        defaults[k] = data[k]
    except Exception as e:  # noqa: BLE001
        logger.warning("load settings failed: %s", e)
    # 规范化
    sp = str(defaults.get("scan_path") or _DEFAULT_SCAN_PATH)
    defaults["scan_path"] = os.path.realpath(os.path.expanduser(sp))
    try:
        defaults["max_depth"] = max(1, min(8, int(defaults.get("max_depth") or 5)))
    except Exception:  # noqa: BLE001
        defaults["max_depth"] = 5
    try:
        defaults["extract_depth"] = max(0, min(4, int(defaults.get("extract_depth") or 2)))
    except Exception:  # noqa: BLE001
        defaults["extract_depth"] = 2
    defaults["userdata_id"] = str(defaults.get("userdata_id") or "").strip()
    defaults["auto_extract"] = bool(defaults.get("auto_extract", True))
    hidden = defaults.get("hidden_exes") or []
    if not isinstance(hidden, list):
        hidden = []
    # 去重规范化
    cleaned = []
    seen = set()
    for h in hidden:
        n = _normalize(str(h)) or str(h).strip()
        if n and n not in seen:
            seen.add(n)
            cleaned.append(n)
    defaults["hidden_exes"] = cleaned
    return defaults


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    import json

    cur = load_settings()
    if "scan_path" in settings and settings["scan_path"]:
        cur["scan_path"] = os.path.realpath(
            os.path.expanduser(str(settings["scan_path"]).strip())
        )
    if "max_depth" in settings:
        try:
            cur["max_depth"] = max(1, min(8, int(settings["max_depth"])))
        except Exception:  # noqa: BLE001
            pass
    if "userdata_id" in settings:
        cur["userdata_id"] = str(settings.get("userdata_id") or "").strip()
    if "auto_extract" in settings:
        cur["auto_extract"] = bool(settings.get("auto_extract"))
    if "extract_depth" in settings:
        try:
            cur["extract_depth"] = max(0, min(4, int(settings["extract_depth"])))
        except Exception:  # noqa: BLE001
            pass
    if "hidden_exes" in settings:
        hidden = settings.get("hidden_exes") or []
        if isinstance(hidden, list):
            cleaned = []
            seen = set()
            for h in hidden:
                n = _normalize(str(h)) or str(h).strip()
                if n and n not in seen:
                    seen.add(n)
                    cleaned.append(n)
            cur["hidden_exes"] = cleaned
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cur, fp, ensure_ascii=False, indent=2)
    return cur


def hide_exes(exes: List[str]) -> Dict[str, Any]:
    cur = load_settings()
    hidden = list(cur.get("hidden_exes") or [])
    seen = set(hidden)
    added = 0
    for e in exes or []:
        n = _normalize(str(e)) or str(e).strip()
        if n and n not in seen:
            hidden.append(n)
            seen.add(n)
            added += 1
    cur["hidden_exes"] = hidden
    save_settings(cur)
    return {"success": True, "added": added, "hidden_count": len(hidden), "hidden_exes": hidden}


def unhide_exes(exes: List[str]) -> Dict[str, Any]:
    cur = load_settings()
    hidden = list(cur.get("hidden_exes") or [])
    remove = set()
    for e in exes or []:
        n = _normalize(str(e)) or str(e).strip()
        if n:
            remove.add(n)
    new_hidden = [h for h in hidden if h not in remove]
    cur["hidden_exes"] = new_hidden
    save_settings(cur)
    return {
        "success": True,
        "removed": len(hidden) - len(new_hidden),
        "hidden_count": len(new_hidden),
        "hidden_exes": new_hidden,
    }


def resolve_primary_userdata_id(preferred: str = "") -> str:
    """选择要写入 shortcuts 的 Steam 用户目录。"""
    root = find_steam_root()
    if not root:
        return ""
    ud_root = os.path.join(root, "userdata")
    if not os.path.isdir(ud_root):
        return ""
    if preferred and os.path.isdir(os.path.join(ud_root, preferred)):
        return preferred

    # loginusers.vdf MostRecent / 唯一用户
    login = os.path.join(root, "config", "loginusers.vdf")
    if os.path.isfile(login):
        try:
            text = open(login, "r", encoding="utf-8", errors="replace").read()
            # SteamID64 -> account id = steamid64 - 76561197960265728
            ids = re.findall(r'"(\d{17})"', text)
            most = re.search(r'"MostRecent"\s*"1"', text)
            # 简单：取第一个 17 位 id 转换
            for sid64 in ids:
                try:
                    acc = str(int(sid64) - 76561197960265728)
                    if os.path.isdir(os.path.join(ud_root, acc)):
                        return acc
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

    # 回退：shortcuts.vdf 最大的用户
    best, best_sz = "", -1
    for sid in os.listdir(ud_root):
        sc = os.path.join(ud_root, sid, "config", "shortcuts.vdf")
        if os.path.isfile(sc):
            sz = os.path.getsize(sc)
            if sz > best_sz:
                best, best_sz = sid, sz
        elif os.path.isdir(os.path.join(ud_root, sid, "config")) and not best:
            best = sid
    return best


def _is_skipped_dir(name: str) -> bool:
    n = name.lower()
    return n in _SKIP_DIR_NAMES or n.startswith(".")


def _is_candidate_filename(name: str) -> bool:
    lower = name.lower()
    if _SKIP_EXE_RE.search(lower):
        return False
    if lower.endswith((".exe", ".appimage")):
        return True
    # 注意：不要把 .bin（光盘镜像等）当成启动器
    if lower.endswith((".x86_64", ".x86")):
        return True
    if lower.endswith(".sh"):
        bad = ("install", "uninstall", "setup", "update", "bootstrap", "prepare", "detect")
        if any(b in lower for b in bad):
            return False
        good = ("launch", "start", "run", "game", "play")
        return any(g in lower for g in good) or lower in ("game.sh", "start.sh", "run.sh")
    return False


def _score_exe(path: str, scan_root: str) -> int:
    """分数越高越像「主启动器」。"""
    name = os.path.basename(path)
    lower = name.lower()
    parent = os.path.basename(os.path.dirname(path)).lower()
    score = 0
    try:
        score += min(int(os.path.getsize(path) // (1024 * 1024)), 200)  # 最大 200
    except Exception:  # noqa: BLE001
        pass
    if lower.endswith(".exe"):
        score += 30
    if lower.endswith(".appimage"):
        score += 40
    if lower in ("game.exe", "start.exe", "play.exe"):
        score += 80
    # 文件名与父目录相似
    stem = re.sub(r"\.(exe|appimage|sh|x86_64|x86|bin)$", "", lower)
    stem = re.sub(r"[^a-z0-9]+", "", stem)
    pstem = re.sub(r"[^a-z0-9]+", "", parent)
    if stem and pstem and (stem in pstem or pstem in stem):
        score += 50
    # 越浅越好
    rel = os.path.relpath(path, scan_root)
    depth = rel.count(os.sep)
    score += max(0, 40 - depth * 8)
    # 降权
    if "updater" in lower or "launcher" in lower and "game" not in lower:
        score -= 15
    if "language" in lower or "config" in lower or "setting" in lower:
        score -= 40
    if "crash" in lower or "report" in lower:
        score -= 100
    return score


def _guess_game_name(exe_path: str, scan_root: str) -> str:
    parent = os.path.basename(os.path.dirname(exe_path))
    # 若父目录是无意义层（如 XJ12345），再往上
    name = parent
    cur = os.path.dirname(exe_path)
    scan_root = os.path.realpath(scan_root)
    for _ in range(3):
        base = os.path.basename(cur)
        if re.fullmatch(r"XJ\d+", base, re.I) or re.fullmatch(r"\d+", base):
            parent2 = os.path.basename(os.path.dirname(cur))
            if parent2 and os.path.realpath(cur) != scan_root:
                cur = os.path.dirname(cur)
                name = os.path.basename(cur) if os.path.realpath(cur) != scan_root else parent
                continue
        # 若当前名太泛
        if base.lower() in ("bin", "binaries", "game", "win64", "win32", "x64", "x86"):
            cur = os.path.dirname(cur)
            if os.path.realpath(cur) == scan_root:
                break
            name = os.path.basename(cur)
            continue
        name = base
        break
    # 回退到 exe 名
    if not name or name in (".", ""):
        name = os.path.splitext(os.path.basename(exe_path))[0]
    return name


def _existing_shortcut_exes() -> set:
    """已添加的 exe 绝对路径集合（去引号）。"""
    out = set()
    # 同步调用列表逻辑太重：直接扫 shortcuts
    root = find_steam_root()
    if not root:
        return out
    ud = os.path.join(root, "userdata")
    if not os.path.isdir(ud):
        return out
    for sid in os.listdir(ud):
        sc = os.path.join(ud, sid, "config", "shortcuts.vdf")
        if not os.path.isfile(sc):
            continue
        try:
            with open(sc, "rb") as fp:
                parsed = _read_node(fp)
        except Exception:  # noqa: BLE001
            continue
        for entry in (parsed.get("shortcuts") or {}).values():
            if not isinstance(entry, dict):
                continue
            n = _normalize(entry.get("Exe") or "")
            if n:
                out.add(n)
    return out


# 支持的压缩格式（按扩展名从长到短匹配）
_ARCHIVE_EXTS = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz2",
    ".txz",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
)


def _is_archive_file(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _ARCHIVE_EXTS)


def _archive_stem(name: str) -> str:
    lower = name.lower()
    for ext in _ARCHIVE_EXTS:
        if lower.endswith(ext):
            return name[: -len(ext)]
    return os.path.splitext(name)[0]


def _dir_nonempty(path: str) -> bool:
    try:
        if not os.path.isdir(path):
            return False
        return any(os.scandir(path))
    except Exception:  # noqa: BLE001
        return False


def _run_cmd(cmd: List[str], timeout: int = 600) -> tuple:
    import subprocess

    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return r.returncode, (r.stderr or b"").decode("utf-8", "replace")[:500]
    except Exception as e:  # noqa: BLE001
        return 99, str(e)


def _extract_one_archive(archive_path: str, dest_dir: str) -> Dict[str, Any]:
    """解压单个压缩包到 dest_dir。优先 7z（兼容 zip/7z/rar/tar.*）。"""
    os.makedirs(dest_dir, exist_ok=True)
    lower = archive_path.lower()
    # 已解压过则跳过
    if _dir_nonempty(dest_dir):
        return {"ok": True, "skipped": True, "dest": dest_dir, "message": "目标已存在，跳过"}

    # 1) 7z / 7za — 最通用
    for bin7 in ("7z", "7za"):
        if shutil.which(bin7):
            code, err = _run_cmd([bin7, "x", "-y", f"-o{dest_dir}", archive_path])
            if code == 0:
                return {"ok": True, "skipped": False, "dest": dest_dir, "tool": bin7}
            logger.warning("7z extract fail %s: %s", archive_path, err)

    # 2) zip → unzip 或 zipfile
    if lower.endswith(".zip"):
        if shutil.which("unzip"):
            code, err = _run_cmd(["unzip", "-o", "-q", archive_path, "-d", dest_dir])
            if code == 0:
                return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "unzip"}
        try:
            import zipfile

            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(dest_dir)
            return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "zipfile"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "dest": dest_dir, "message": f"zip 解压失败: {e}"}

    # 3) rar → unrar
    if lower.endswith(".rar") and shutil.which("unrar"):
        code, err = _run_cmd(["unrar", "x", "-o+", archive_path, dest_dir + "/"])
        if code == 0:
            return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "unrar"}
        return {"ok": False, "dest": dest_dir, "message": err}

    # 4) tar 系列
    if any(lower.endswith(x) for x in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        if shutil.which("tar"):
            code, err = _run_cmd(["tar", "-xf", archive_path, "-C", dest_dir])
            if code == 0:
                return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "tar"}
        try:
            import tarfile

            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(dest_dir)
            return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "tarfile"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "dest": dest_dir, "message": f"tar 解压失败: {e}"}

    return {"ok": False, "dest": dest_dir, "message": "无可用解压工具或不支持的格式"}


def extract_archives_in_tree(
    scan_path: str,
    max_walk_depth: int = 5,
    nest_depth: int = 2,
) -> Dict[str, Any]:
    """在扫描目录内查找压缩包并递归解压（嵌套最多 nest_depth 层）。"""
    scan_path = os.path.realpath(os.path.expanduser(scan_path))
    extracted: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    skipped = 0

    for level in range(max(0, nest_depth)):
        archives: List[str] = []
        for dirpath, dirnames, filenames in os.walk(scan_path):
            rel = os.path.relpath(dirpath, scan_path)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > max_walk_depth + level:  # 解压后可能更深
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]
            for fn in filenames:
                if _is_archive_file(fn):
                    archives.append(os.path.join(dirpath, fn))

        if not archives:
            break

        level_did = 0
        for ap in archives:
            stem = _archive_stem(os.path.basename(ap))
            # 解压到同目录下的「去掉扩展名」文件夹
            dest = os.path.join(os.path.dirname(ap), stem)
            # 避免解压到自身内部造成循环：若 dest 就是某种奇怪路径则跳过
            if os.path.commonpath([os.path.realpath(ap), os.path.realpath(dest + os.sep)]) == os.path.realpath(ap):
                continue
            # 标记：同目录已有同名文件夹且非空 → 视为已解压
            if _dir_nonempty(dest):
                skipped += 1
                continue
            # 体积保护：> 40GB 跳过
            try:
                if os.path.getsize(ap) > 40 * 1024 * 1024 * 1024:
                    failed.append({"archive": ap, "message": "文件过大(>40GB)，已跳过"})
                    continue
            except Exception:  # noqa: BLE001
                pass

            logger.info("extracting L%d %s -> %s", level, ap, dest)
            try:
                result = _extract_one_archive(ap, dest)
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "dest": dest, "message": str(e)}
            if result.get("ok"):
                if result.get("skipped"):
                    skipped += 1
                else:
                    level_did += 1
                    extracted.append({"archive": ap, "dest": dest, "level": level, **result})
            else:
                failed.append({"archive": ap, "message": result.get("message") or "fail"})

        if level_did == 0:
            break  # 本层没有新解压，停止递归

    return {
        "extracted": extracted,
        "failed": failed,
        "skipped_existing": skipped,
        "extracted_count": len(extracted),
        "failed_count": len(failed),
    }


def scan_folder_for_games(
    scan_path: str,
    max_depth: int = 5,
    auto_extract: bool = True,
    extract_depth: int = 2,
    include_hidden: bool = False,
) -> Dict[str, Any]:
    """扫描目录；可选先解压压缩包；返回启动项列表（含 hidden 标记）。"""
    scan_path = os.path.realpath(os.path.expanduser(scan_path or _DEFAULT_SCAN_PATH))
    if not os.path.isdir(scan_path):
        return {"games": [], "extract": None, "scan_path": scan_path}

    extract_info = None
    if auto_extract and extract_depth > 0:
        try:
            extract_info = extract_archives_in_tree(
                scan_path, max_walk_depth=max_depth, nest_depth=extract_depth
            )
            logger.info(
                "extract done: +%s fail=%s skip=%s",
                extract_info.get("extracted_count"),
                extract_info.get("failed_count"),
                extract_info.get("skipped_existing"),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("extract_archives failed: %s", e)
            extract_info = {"extracted_count": 0, "failed_count": 1, "error": str(e)}

    settings = load_settings()
    hidden_set = set(settings.get("hidden_exes") or [])
    existing = _existing_shortcut_exes()
    found: List[Dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(scan_path):
        rel = os.path.relpath(dirpath, scan_path)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        # 解压后允许稍深一点
        limit = max_depth + (2 if auto_extract else 0)
        if depth > limit:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]

        for fn in filenames:
            if not _is_candidate_filename(fn):
                continue
            full = os.path.join(dirpath, fn)
            if not os.path.isfile(full):
                continue
            try:
                sz = os.path.getsize(full)
            except Exception:  # noqa: BLE001
                sz = 0
            lower = fn.lower()
            if lower.endswith(".exe") and sz < 50 * 1024:
                continue
            if lower.endswith(".sh") and sz > 2 * 1024 * 1024:
                continue
            exe_n = os.path.realpath(full)
            start = os.path.dirname(exe_n) + os.sep
            name = _guess_game_name(exe_n, scan_path)
            score = _score_exe(exe_n, scan_path)
            is_hidden = exe_n in hidden_set
            found.append(
                {
                    "id": f"{score}:{exe_n}",
                    "name": name,
                    "exe": exe_n,
                    "start_dir": start,
                    "size": sz,
                    "score": score,
                    "already_added": exe_n in existing,
                    "hidden": is_hidden,
                    "rel_dir": os.path.relpath(os.path.dirname(exe_n), scan_path),
                }
            )

    by_dir: Dict[str, List[Dict[str, Any]]] = {}
    for item in found:
        by_dir.setdefault(item["start_dir"], []).append(item)
    pruned: List[Dict[str, Any]] = []
    for _dir, items in by_dir.items():
        items.sort(key=lambda x: (-x["score"], x["name"]))
        pruned.extend(items[:2])

    # 默认列表：不展示 hidden（除非 include_hidden）
    if not include_hidden:
        visible = [x for x in pruned if not x.get("hidden")]
        hidden_only = [x for x in pruned if x.get("hidden")]
    else:
        visible = pruned
        hidden_only = [x for x in pruned if x.get("hidden")]

    visible.sort(key=lambda x: (x["already_added"], -x["score"], x["name"].lower()))
    hidden_only.sort(key=lambda x: (-x["score"], x["name"].lower()))

    return {
        "games": visible[:300],
        "hidden_games": hidden_only[:300],
        "extract": extract_info,
        "scan_path": scan_path,
        "hidden_count_settings": len(hidden_set),
    }


def _format_exe_for_steam(exe: str) -> str:
    """Steam shortcuts Exe 字段通常带双引号。"""
    exe = (_normalize(exe) or exe).replace("\\", "/")
    if not exe.startswith('"'):
        exe = f'"{exe}"'
    return exe


def _format_startdir_for_steam(start_dir: str) -> str:
    d = (_normalize(start_dir) or start_dir).replace("\\", "/")
    if not d.endswith("/"):
        d = d + "/"
    return d


def add_games_to_steam(
    entries: List[Dict[str, Any]],
    userdata_id: str = "",
) -> Dict[str, Any]:
    """将勾选的游戏写入 shortcuts.vdf。"""
    root = find_steam_root()
    if not root:
        return {"success": False, "message": "找不到 Steam 目录", "added": [], "skipped": []}

    settings = load_settings()
    sid = resolve_primary_userdata_id(userdata_id or settings.get("userdata_id") or "")
    if not sid:
        return {"success": False, "message": "找不到 Steam 用户目录", "added": [], "skipped": []}

    sc_path = os.path.join(root, "userdata", sid, "config", "shortcuts.vdf")
    os.makedirs(os.path.dirname(sc_path), exist_ok=True)

    if os.path.isfile(sc_path):
        try:
            with open(sc_path, "rb") as fp:
                parsed = _read_node(fp)
        except Exception as e:  # noqa: BLE001
            logger.error("read shortcuts failed: %s", e)
            parsed = {"shortcuts": {}}
    else:
        parsed = {"shortcuts": {}}

    shortcuts = parsed.setdefault("shortcuts", {})
    if not isinstance(shortcuts, dict):
        shortcuts = {}
        parsed["shortcuts"] = shortcuts

    # 已有 exe
    existing_exes = set()
    for entry in shortcuts.values():
        if isinstance(entry, dict):
            n = _normalize(entry.get("Exe") or "")
            if n:
                existing_exes.add(n)

    # 下一 key
    max_key = -1
    for k in shortcuts.keys():
        try:
            max_key = max(max_key, int(k))
        except Exception:  # noqa: BLE001
            pass
    next_key = max_key + 1

    added = []
    skipped = []
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        exe_path = _normalize(raw.get("exe") or "")
        name = str(raw.get("name") or "").strip()
        start = _normalize(raw.get("start_dir") or "") or (
            os.path.dirname(exe_path) if exe_path else ""
        )
        if not exe_path or not os.path.isfile(exe_path):
            skipped.append({"exe": raw.get("exe"), "reason": "文件不存在"})
            continue
        if not name:
            name = os.path.splitext(os.path.basename(exe_path))[0]
        if exe_path in existing_exes:
            skipped.append({"exe": exe_path, "name": name, "reason": "已在库中"})
            continue

        appid = compute_appid(exe_path, name)
        steamexe = _format_exe_for_steam(exe_path)
        steamdir = _format_startdir_for_steam(start)

        entry = {
            "appid": appid_to_steam_int32(appid),
            "AppName": name,
            "Exe": steamexe,
            "StartDir": steamdir,
            "icon": "",
            "ShortcutPath": "",
            "LaunchOptions": "",
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "OpenVR": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "DevkitOverrideAppID": 0,
            "LastPlayTime": 0,
            "FlatpakAppID": "",
            "tags": {},
        }
        key = str(next_key)
        shortcuts[key] = entry
        next_key += 1
        existing_exes.add(exe_path)
        added.append(
            {
                "key": key,
                "appid": appid,
                "name": name,
                "exe": steamexe,
                "start_dir": steamdir,
                "userdata_id": sid,
            }
        )

    if added:
        try:
            # 备份
            if os.path.isfile(sc_path):
                shutil.copy2(sc_path, sc_path + f".bak_nsc_add_{int(__import__('time').time())}")
            write_vdf(sc_path, parsed)
        except Exception as e:  # noqa: BLE001
            logger.error("write shortcuts failed: %s", e)
            return {
                "success": False,
                "message": f"写入 shortcuts.vdf 失败: {e}",
                "added": [],
                "skipped": skipped,
            }

    return {
        "success": True,
        "message": f"已添加 {len(added)} 个，跳过 {len(skipped)} 个。请重启 Steam 后在库中可见。",
        "added": added,
        "skipped": skipped,
        "userdata_id": sid,
        "shortcuts_path": sc_path,
    }


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------
class Plugin:
    async def _main(self):
        logger.info("NonSteamCleaner started")

    async def _unload(self):
        logger.info("NonSteamCleaner unloaded")

    # ---- 扫描设置 ----
    async def get_scan_settings(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        s = load_settings()
        s["default_scan_path"] = _DEFAULT_SCAN_PATH
        s["resolved_userdata_id"] = resolve_primary_userdata_id(s.get("userdata_id") or "")
        s["hidden_count"] = len(s.get("hidden_exes") or [])
        return s

    async def set_scan_settings(self, settings: Any = None, **kwargs: Any) -> Dict[str, Any]:
        if isinstance(settings, dict):
            data = settings
        elif kwargs:
            data = kwargs
        else:
            data = {}
        return save_settings(data)

    async def hide_scan_items(self, exes: Any = None, **kwargs: Any) -> Dict[str, Any]:
        if isinstance(exes, dict):
            kwargs = {**exes, **kwargs}
            exes = kwargs.get("exes")
        if exes is None:
            exes = kwargs.get("exes") or []
        if not isinstance(exes, list):
            return {"success": False, "message": "exes 必须是路径数组"}
        return hide_exes(exes)

    async def unhide_scan_items(self, exes: Any = None, **kwargs: Any) -> Dict[str, Any]:
        if isinstance(exes, dict):
            kwargs = {**exes, **kwargs}
            exes = kwargs.get("exes")
        if exes is None:
            exes = kwargs.get("exes") or []
        if not isinstance(exes, list):
            return {"success": False, "message": "exes 必须是路径数组"}
        return unhide_exes(exes)

    async def get_hidden_scan_items(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        s = load_settings()
        hidden = s.get("hidden_exes") or []
        items = []
        for h in hidden:
            items.append(
                {
                    "exe": h,
                    "name": os.path.basename(h) if h else "",
                    "exists": os.path.isfile(h) if h else False,
                }
            )
        return {"success": True, "hidden_exes": hidden, "items": items, "count": len(items)}

    async def scan_download_games(
        self,
        scan_path: str = "",
        max_depth: int = 0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if isinstance(scan_path, dict):
            kwargs = {**scan_path, **kwargs}
            scan_path = kwargs.get("scan_path", "")
            max_depth = kwargs.get("max_depth", max_depth)
        settings = load_settings()
        path = str(scan_path or kwargs.get("scan_path") or settings.get("scan_path") or _DEFAULT_SCAN_PATH)
        try:
            depth = int(max_depth or kwargs.get("max_depth") or settings.get("max_depth") or 5)
        except Exception:  # noqa: BLE001
            depth = 5
        depth = max(1, min(8, depth))
        auto_extract = settings.get("auto_extract", True)
        if "auto_extract" in kwargs:
            auto_extract = bool(kwargs.get("auto_extract"))
        try:
            extract_depth = int(
                kwargs.get("extract_depth", settings.get("extract_depth", 2)) or 2
            )
        except Exception:  # noqa: BLE001
            extract_depth = 2
        include_hidden = bool(kwargs.get("include_hidden", False))

        if not os.path.isdir(os.path.expanduser(path)):
            return {
                "success": False,
                "message": f"目录不存在: {path}",
                "games": [],
                "hidden_games": [],
                "scan_path": path,
            }
        result = scan_folder_for_games(
            path,
            depth,
            auto_extract=bool(auto_extract),
            extract_depth=extract_depth,
            include_hidden=include_hidden,
        )
        games = result.get("games") or []
        hidden_games = result.get("hidden_games") or []
        extract = result.get("extract") or {}
        parts = [f"扫描到 {len(games)} 个启动器"]
        if hidden_games:
            parts.append(f"隐藏栏 {len(hidden_games)} 个")
        if extract:
            ec = extract.get("extracted_count") or 0
            fc = extract.get("failed_count") or 0
            sc = extract.get("skipped_existing") or 0
            if ec or fc or sc:
                parts.append(f"解压新{ec}/跳过{sc}/失败{fc}")
        return {
            "success": True,
            "message": "，".join(parts),
            "games": games,
            "hidden_games": hidden_games,
            "extract": extract,
            "scan_path": result.get("scan_path") or os.path.realpath(os.path.expanduser(path)),
            "max_depth": depth,
            "auto_extract": bool(auto_extract),
            "extract_depth": extract_depth,
        }

    async def add_non_steam_games(self, entries: Any = None, userdata_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        if isinstance(entries, dict) and "entries" in entries:
            kwargs = {**entries, **kwargs}
            entries = kwargs.get("entries")
            userdata_id = kwargs.get("userdata_id", userdata_id)
        if entries is None:
            entries = kwargs.get("entries") or []
        if not isinstance(entries, list):
            return {"success": False, "message": "entries 必须是数组", "added": [], "skipped": []}
        return add_games_to_steam(entries, userdata_id=str(userdata_id or kwargs.get("userdata_id") or ""))

    async def find_missing_nonsteam_games(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """检测库中非 Steam 游戏的启动文件是否还存在。"""
        # 兼容 callable({}) 把空对象当位置参数传入
        games = await self.get_non_steam_games()
        missing: List[Dict[str, Any]] = []
        ok_count = 0
        for g in games:
            exe_raw = g.get("exe") or ""
            start_raw = g.get("start_dir") or ""
            exe_n = _normalize(exe_raw)
            start_n = _normalize(start_raw)
            exe_exists = bool(exe_n and os.path.isfile(exe_n))
            # 无 exe 时若 start 目录也不在，同样视为失效
            start_exists = bool(start_n and os.path.isdir(start_n))
            if exe_exists:
                ok_count += 1
                continue
            reason = "exe_missing"
            if not exe_n:
                reason = "exe_empty"
            elif not start_exists:
                reason = "exe_and_startdir_missing"
            missing.append(
                {
                    "appid": normalize_appid(g.get("appid")),
                    "name": g.get("name") or "",
                    "exe": exe_raw,
                    "normalized_exe": exe_n or "",
                    "start_dir": start_raw,
                    "normalized_start": start_n or "",
                    "userdata_id": g.get("userdata_id") or "",
                    "key": str(g.get("key") if g.get("key") is not None else ""),
                    "reason": reason,
                    "exe_exists": exe_exists,
                    "start_exists": start_exists,
                }
            )
        return {
            "success": True,
            "total": len(games),
            "ok_count": ok_count,
            "missing_count": len(missing),
            "missing": missing,
            "message": f"共 {len(games)} 个非 Steam，失效 {len(missing)} 个，正常 {ok_count} 个",
        }

    async def purge_missing_nonsteam_games(
        self,
        entries: Any = None,
        purge_all_missing: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """从 Steam 库移除启动文件已不存在的非 Steam 快捷方式（不删本体，本体本就不存在）。"""
        if isinstance(entries, dict) and (
            "entries" in entries or "purge_all_missing" in entries or "appid" in entries
        ):
            kwargs = {**entries, **kwargs}
            entries = kwargs.get("entries", entries if "appid" in entries else None)
            purge_all_missing = bool(kwargs.get("purge_all_missing", purge_all_missing))

        if purge_all_missing or entries is None and kwargs.get("purge_all_missing"):
            found = await self.find_missing_nonsteam_games()
            to_purge = found.get("missing") or []
        else:
            if entries is None:
                entries = kwargs.get("entries") or []
            if not isinstance(entries, list):
                return {
                    "success": False,
                    "message": "entries 必须是数组，或设 purge_all_missing=true",
                    "removed": [],
                    "failed": [],
                }
            to_purge = entries

        removed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for item in to_purge:
            if not isinstance(item, dict):
                continue
            try:
                rm = remove_shortcuts_from_steam(
                    userdata_id=str(item.get("userdata_id") or ""),
                    key=str(item.get("key") if item.get("key") is not None else ""),
                    appid=item.get("appid") or 0,
                    exe=str(item.get("exe") or item.get("normalized_exe") or ""),
                    name=str(item.get("name") or ""),
                )
                if rm.get("removed"):
                    removed.append(
                        {
                            "name": item.get("name"),
                            "appid": normalize_appid(item.get("appid")),
                            "count": rm.get("removed_count"),
                            "details": rm.get("details"),
                        }
                    )
                else:
                    failed.append(
                        {
                            "name": item.get("name"),
                            "appid": item.get("appid"),
                            "message": rm.get("message"),
                        }
                    )
            except Exception as e:  # noqa: BLE001
                logger.error("purge missing failed: %s", e)
                failed.append({"name": item.get("name"), "error": str(e)})

        return {
            "success": True,
            "removed_count": len(removed),
            "failed_count": len(failed),
            "removed": removed,
            "failed": failed,
            "message": (
                f"已从 Steam 移除 {len(removed)} 个失效快捷方式"
                + (f"，失败 {len(failed)} 个" if failed else "")
                + "。请完全退出 Steam 再打开以刷新库。"
            ),
            "hint": "若库中仍显示，请完全退出 Steam 再启动（运行中可能缓存库列表）。",
        }

    # ---- 列出所有非 Steam 游戏 ----
    async def get_non_steam_games(self) -> List[Dict[str, Any]]:
        root = find_steam_root()
        if not root:
            return []
        results: List[Dict[str, Any]] = []
        userdata = os.path.join(root, "userdata")
        if not os.path.isdir(userdata):
            return []

        for sid in sorted(os.listdir(userdata)):
            sc_path = os.path.join(userdata, sid, "config", "shortcuts.vdf")
            if not os.path.isfile(sc_path):
                continue
            try:
                with open(sc_path, "rb") as fp:
                    parsed = _read_node(fp)
            except Exception as e:  # noqa: BLE001
                logger.error("解析 shortcuts 失败 %s: %s", sc_path, e)
                continue

            shortcuts = parsed.get("shortcuts", {})
            if not isinstance(shortcuts, dict):
                continue
            for key, entry in shortcuts.items():
                if not isinstance(entry, dict):
                    continue
                exe = entry.get("Exe") or ""
                if not exe:
                    continue
                name = entry.get("AppName") or ""
                appid_raw = entry.get("appid")
                if appid_raw is not None:
                    try:
                        appid = int(appid_raw)
                    except (TypeError, ValueError):
                        appid = compute_appid(exe, name)
                else:
                    appid = compute_appid(exe, name)
                results.append(
                    {
                        "appid": normalize_appid(appid),
                        "name": name,
                        "exe": exe,
                        "start_dir": entry.get("StartDir") or "",
                        "userdata_id": sid,
                        "key": key,
                    }
                )
        return results

    async def get_game_by_appid(self, appid: int = 0, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """按 appid 查找非 Steam 游戏（供库页面/右键菜单调用）。"""
        if kwargs and not appid:
            appid = kwargs.get("appid", 0)
        target = normalize_appid(appid)
        if not target:
            return None
        games = await self.get_non_steam_games()
        for g in games:
            if normalize_appid(g.get("appid")) == target:
                return g
        return None

    # ---- 计算将要删除的目标路径 ----
    def _compute_targets(
        self,
        game: Dict[str, Any],
        delete_body: bool,
        delete_saves: bool,
        delete_shader: bool,
    ) -> List[str]:
        targets: List[str] = []
        appid = normalize_appid(game.get("appid"))
        sid = str(game.get("userdata_id") or "")

        # 兼容布尔从 JSON/callable 传来的各种形式
        def _truthy(v: Any) -> bool:
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return bool(v)

        delete_body = _truthy(delete_body)
        delete_saves = _truthy(delete_saves)
        delete_shader = _truthy(delete_shader)

        if delete_body:
            exe = _normalize(game.get("exe", "") or "")
            start = _normalize(game.get("start_dir", "") or "")
            logger.info("compute body paths exe=%s start=%s", exe, start)

            # StartDir：仅当它足够“像游戏目录”时才整目录删除
            if start and os.path.isdir(start) and _safe_to_delete(start):
                targets.append(start)
            # 可执行文件：始终尝试（若已在 start 目录内则不必重复）
            if exe and os.path.exists(exe) and _safe_to_delete(exe):
                if not start or not exe.startswith(start.rstrip("/") + "/"):
                    if exe not in targets:
                        targets.append(exe)
                elif start not in targets:
                    # start 未加入（不安全）时至少删 exe
                    targets.append(exe)
            elif exe:
                logger.warning("exe 不存在或不可删: %s", exe)

        if delete_saves:
            for cd in _collect_prefix_dirs(appid, "compatdata"):
                if _safe_to_delete(cd):
                    targets.append(cd)

        if delete_shader:
            for sc in _collect_prefix_dirs(appid, "shadercache"):
                if _safe_to_delete(sc):
                    targets.append(sc)

        # 网格图：移除快捷方式时一并清
        for f in _collect_grid_files(sid, appid):
            if os.path.exists(f) and _safe_to_delete(f):
                targets.append(f)

        # 去重
        seen = set()
        out: List[str] = []
        for t in targets:
            rt = os.path.realpath(t) if os.path.exists(t) else t
            if rt not in seen:
                seen.add(rt)
                out.append(t)
        logger.info(
            "targets body=%s saves=%s shader=%s -> %s",
            delete_body,
            delete_saves,
            delete_shader,
            out,
        )
        return out

    async def preview_delete(
        self,
        appid: Any = 0,
        userdata_id: str = "",
        exe: str = "",
        start_dir: str = "",
        delete_body: bool = False,
        delete_saves: bool = False,
        delete_shader: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # 兼容：callable 传入单个 dict / 或 **kwargs
        if isinstance(appid, dict):
            kwargs = {**appid, **kwargs}
            appid = kwargs.get("appid", 0)
        if kwargs:
            userdata_id = kwargs.get("userdata_id", userdata_id)
            exe = kwargs.get("exe", exe)
            start_dir = kwargs.get("start_dir", start_dir)
            delete_body = kwargs.get("delete_body", delete_body)
            delete_saves = kwargs.get("delete_saves", delete_saves)
            delete_shader = kwargs.get("delete_shader", delete_shader)
            if "appid" in kwargs and not isinstance(kwargs.get("appid"), dict):
                appid = kwargs.get("appid", appid)
        game = {
            "appid": normalize_appid(appid),
            "userdata_id": str(userdata_id or ""),
            "exe": exe or "",
            "start_dir": start_dir or "",
        }
        targets = self._compute_targets(game, delete_body, delete_saves, delete_shader)
        existing = [t for t in targets if os.path.lexists(t)]
        return {
            "targets": targets,
            "existing": existing,
            "normalized_exe": _normalize(exe or ""),
            "normalized_start": _normalize(start_dir or ""),
            "appid": game["appid"],
        }

    async def delete_non_steam_game(
        self,
        appid: Any = 0,
        userdata_id: str = "",
        key: str = "",
        exe: str = "",
        start_dir: str = "",
        delete_body: bool = False,
        delete_saves: bool = False,
        delete_shader: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if isinstance(appid, dict):
            kwargs = {**appid, **kwargs}
            appid = kwargs.get("appid", 0)
        if kwargs:
            userdata_id = kwargs.get("userdata_id", userdata_id)
            key = kwargs.get("key", key)
            exe = kwargs.get("exe", exe)
            start_dir = kwargs.get("start_dir", start_dir)
            delete_body = kwargs.get("delete_body", delete_body)
            delete_saves = kwargs.get("delete_saves", delete_saves)
            delete_shader = kwargs.get("delete_shader", delete_shader)
            if "appid" in kwargs and not isinstance(kwargs.get("appid"), dict):
                appid = kwargs.get("appid", appid)

        root = find_steam_root()
        game = {
            "appid": normalize_appid(appid),
            "userdata_id": str(userdata_id or ""),
            "exe": exe or "",
            "start_dir": start_dir or "",
        }
        logger.info(
            "delete request appid=%s key=%s body=%s saves=%s shader=%s exe=%s start=%s",
            game["appid"],
            key,
            delete_body,
            delete_saves,
            delete_shader,
            exe,
            start_dir,
        )
        targets = self._compute_targets(game, delete_body, delete_saves, delete_shader)

        deleted: List[str] = []
        failed: List[str] = []
        for t in targets:
            try:
                if not os.path.lexists(t):
                    continue
                if os.path.islink(t) or os.path.isfile(t):
                    os.unlink(t)
                    deleted.append(t)
                elif os.path.isdir(t):
                    shutil.rmtree(t)
                    deleted.append(t)
            except Exception as e:  # noqa: BLE001
                logger.error("删除失败 %s: %s", t, e)
                failed.append(f"{t}: {e}")

        # 从所有用户 shortcuts.vdf 移除（key/appid/exe 多重匹配 + 重排 + 校验）
        rm = remove_shortcuts_from_steam(
            userdata_id=str(userdata_id or ""),
            key=str(key or ""),
            appid=game["appid"],
            exe=str(exe or game.get("exe") or ""),
            name="",  # 不用纯名称，避免误伤同名
        )
        removed_shortcut = bool(rm.get("removed"))
        if not removed_shortcut:
            failed.append(rm.get("message") or "shortcuts 未移除")
            logger.warning("shortcut remove failed: %s", rm)
        else:
            logger.info("shortcut remove ok: %s", rm)

        result = {
            "deleted": deleted,
            "failed": failed,
            "removed_shortcut": removed_shortcut,
            "removed_shortcut_count": rm.get("removed_count", 0),
            "shortcut_details": rm.get("details") or [],
            "targets": targets,
            "count": len(deleted),
            "hint": (
                "若库中仍显示，请完全退出 Steam 再打开（Steam 运行中可能用内存缓存覆盖 shortcuts）。"
                if removed_shortcut
                else "未能改写 shortcuts.vdf，请完全退出 Steam 后重试清理。"
            ),
        }
        logger.info("delete result %s", result)
        return result
