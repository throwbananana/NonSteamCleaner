"""
NonSteamCleaner - 彻底清理 Steam 中添加的非 Steam 游戏 (Decky Loader 后端)

功能:
  - 读取所有用户的 shortcuts.vdf，列出已添加的非 Steam 游戏
  - 提供预览将要删除的文件路径（含 Steam 运行中 / 共用目录警告）
  - 按选项删除:
        1) 本体
        2) 本体 + 存档 (compatdata 前缀，包含 Proton 前缀与存档)
        3) 本体 + 存档 + 着色器缓存
        4) 本体 + 着色器缓存
  - 同时清理 Steam 库中的快捷方式(shortcuts.vdf)以及对应的网格图片
  - 扫描添加、失效/重复快捷方式、截图设图标
  - 修复汉化字体：为非 Steam 游戏设置中/日/繁 Proton 区域、黑体映射与 LANG 启动项

注意:
  - 删除操作不可恢复，前端会有二次确认。
  - 修改 shortcuts.vdf 后需要重启 Steam 才能在库中消失。
  - "本体"会删除可执行文件及其 StartDir 所在的游戏目录。
  - "存档"通过删除 compatdata/<appid> 前缀实现，这同时会删除 Proton 前缀(注册表/配置)。
"""

import os
import re
import shutil
import stat
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

def _load_cjk_font_repair():
    """加载 cjk_font_repair：同目录 / 数据目录 / 开发目录。"""
    import importlib.util
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "cjk_font_repair.py"),
        os.path.expanduser("~/.local/share/decky-loader/plugins/NonSteamCleaner/cjk_font_repair.py"),
        os.path.expanduser("~/homebrew/data/NonSteamCleaner/cjk_font_repair.py"),
        os.path.expanduser("~/homebrew/plugins/NonSteamCleaner/cjk_font_repair.py"),
        os.path.expanduser("~/nonsteam-cleaner/cjk_font_repair.py"),
        "/home/deck/homebrew/data/NonSteamCleaner/cjk_font_repair.py",
        "/home/deck/nonsteam-cleaner/cjk_font_repair.py",
    ]
    # 也允许已在 sys.path 的常规 import
    # 禁止先 import 到旧的 data 目录副本（8 月那份没有 MINGLAN 逻辑）
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("cjk_font_repair", path)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules["cjk_font_repair"] = mod
            spec.loader.exec_module(mod)
            return mod
        except Exception as e:  # noqa: BLE001
            logger.warning("load cjk_font_repair from %s failed: %s", path, e)
    raise ImportError("cjk_font_repair.py not found")


_cjk = _load_cjk_font_repair()
CJK_LANG_PRESETS = _cjk.CJK_LANG_PRESETS
CJK_FONT_SIZE_OPTIONS = getattr(_cjk, "CJK_FONT_SIZE_OPTIONS", [])
repair_cjk_fonts_batch = _cjk.repair_cjk_fonts_batch
repair_cjk_fonts_for_game = _cjk.repair_cjk_fonts_for_game
resolve_cjk_preset = _cjk.resolve_cjk_preset

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
    os.path.expanduser("~/Downloads/installed"),
    "/home/deck/Downloads/installed",
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


def is_steam_running() -> bool:
    """Steam 客户端是否在跑（改 shortcuts 后可能被内存缓存盖回）。"""
    for p in (
        os.path.expanduser("~/.steampid"),
        os.path.expanduser("~/.steam/steam.pid"),
    ):
        if not os.path.isfile(p):
            continue
        try:
            pid = int(open(p, "r", encoding="utf-8", errors="replace").read().strip())
        except Exception:  # noqa: BLE001
            continue
        if pid > 1 and os.path.exists(f"/proc/{pid}"):
            return True
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                comm = open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace").read().strip()
            except Exception:  # noqa: BLE001
                continue
            if comm in ("steam", "steamwebhelper"):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _find_steam_client_pid() -> int:
    """定位 Steam 主客户端进程 pid（不是 steamwebhelper 子进程），用于强制重启。"""
    for p in (
        os.path.expanduser("~/.steampid"),
        os.path.expanduser("~/.steam/steam.pid"),
    ):
        if not os.path.isfile(p):
            continue
        try:
            pid = int(open(p, "r", encoding="utf-8", errors="replace").read().strip())
        except Exception:  # noqa: BLE001
            continue
        if pid > 1 and os.path.exists(f"/proc/{pid}"):
            return pid
    try:
        for pid_s in os.listdir("/proc"):
            if not pid_s.isdigit():
                continue
            try:
                comm = open(f"/proc/{pid_s}/comm", "r", encoding="utf-8", errors="replace").read().strip()
            except Exception:  # noqa: BLE001
                continue
            if comm == "steam":
                return int(pid_s)
    except Exception:  # noqa: BLE001
        pass
    return 0


def restart_steam_client() -> Dict[str, Any]:
    """强制结束 Steam 客户端进程。

    Steam 把 shortcuts.vdf/注册表等改动读进内存后不会自动感知外部修改；它自己退出
    （包括整机重启时的正常关闭流程）时会把内存里那份旧数据重新写回磁盘，把插件刚
    做的改动盖回去。普通"退出再打开"走的是 Steam 自己的优雅退出流程，一样会先把
    旧数据存盘再关，所以改动一样保不住。这里改用 SIGKILL 跳过 Steam 自己的退出前
    保存，Gaming Mode 下 gamescope 会话通常会自动把 Steam 重新拉起来；桌面模式下
    杀掉后不一定会自动重开，需要用户自己手动启动一次 Steam。
    """
    pid = _find_steam_client_pid()
    if not pid:
        return {"success": False, "message": "未检测到正在运行的 Steam 客户端进程"}
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return {"success": False, "message": "Steam 进程在重启前已经退出"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"重启失败: {e}"}
    return {
        "success": True,
        "message": (
            "已强制结束 Steam 客户端。Gaming Mode 下会自动重新拉起；"
            "如果是桌面模式且没有自动重开，请手动启动 Steam。"
        ),
    }


# ---------------------------------------------------------------------------
# 备份文件管理
#
# 删除/去重/添加/改语言写 shortcuts.vdf 之前，以及修字体改 system.reg/user.reg
# 之前，各处都会先备份成 <原文件>.bak_nsc* （具体后缀因功能而异，但都以
# ".bak_nsc" 开头）。这些备份一直在磁盘上，只是从来没有清理、也没有入口拿来恢复。
# 这里只覆盖"已知安全范围"——Steam userdata 的 shortcuts.vdf 和各 compatdata
# 前缀的 system.reg/user.reg；游戏目录内的字体备份（Fonts/*.bak_nsc*）散落在
# 用户自选的任意扫描目录里，没有可枚举的安全范围，不纳入这里。
# ---------------------------------------------------------------------------
_BAK_SUFFIX_RE = re.compile(r"\.bak_nsc\w*$")


def _iter_known_backup_files() -> List[str]:
    out: List[str] = []
    root = find_steam_root()
    if not root:
        return out

    ud_root = os.path.join(root, "userdata")
    if os.path.isdir(ud_root):
        for sid in os.listdir(ud_root):
            cfg_dir = os.path.join(ud_root, sid, "config")
            if not os.path.isdir(cfg_dir):
                continue
            try:
                for fn in os.listdir(cfg_dir):
                    if fn.startswith("shortcuts.vdf") and _BAK_SUFFIX_RE.search(fn):
                        out.append(os.path.join(cfg_dir, fn))
            except Exception:  # noqa: BLE001
                continue

    for lib_root in iter_steam_library_roots():
        compat = os.path.join(lib_root, "steamapps", "compatdata")
        if not os.path.isdir(compat):
            continue
        try:
            appids = os.listdir(compat)
        except Exception:  # noqa: BLE001
            continue
        for aid in appids:
            pfx = os.path.join(compat, aid, "pfx")
            if not os.path.isdir(pfx):
                continue
            try:
                for fn in os.listdir(pfx):
                    if fn.startswith(("system.reg", "user.reg")) and _BAK_SUFFIX_RE.search(fn):
                        out.append(os.path.join(pfx, fn))
            except Exception:  # noqa: BLE001
                continue
    return out


def list_backup_files() -> List[Dict[str, Any]]:
    """列出已知范围内的备份文件，最新的在前。"""
    out: List[Dict[str, Any]] = []
    for p in _iter_known_backup_files():
        try:
            st = os.stat(p)
        except Exception:  # noqa: BLE001
            continue
        original = _BAK_SUFFIX_RE.sub("", p)
        out.append(
            {
                "path": p,
                "original": original,
                "original_exists": os.path.isfile(original),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            }
        )
    out.sort(key=lambda x: -x["mtime"])
    return out[:300]


def restore_backup_file(path: str) -> Dict[str, Any]:
    """把某个备份文件恢复回原路径。只接受已知安全范围内、命名匹配的备份。"""
    p = _normalize(path) or str(path or "")
    if not p or not os.path.isfile(p):
        return {"success": False, "message": "备份文件不存在"}
    if not _BAK_SUFFIX_RE.search(os.path.basename(p)):
        return {"success": False, "message": "不是本插件产生的备份文件，拒绝操作"}
    known = set(_iter_known_backup_files())
    if p not in known:
        return {"success": False, "message": "该备份不在已知安全范围内，拒绝操作"}

    original = _BAK_SUFFIX_RE.sub("", p)
    try:
        if os.path.isfile(original):
            import time as _time

            shutil.copy2(
                original,
                original + f".bak_nsc_before_restore_{int(_time.time())}",
            )
        shutil.copy2(p, original)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"恢复失败: {e}"}
    return {
        "success": True,
        "original": original,
        "message": f"已恢复 {os.path.basename(original)}。请完全退出 Steam 再打开以生效。",
    }


def cleanup_backup_files(keep_latest: int = 3, older_than_days: int = 14) -> Dict[str, Any]:
    """清理旧备份：每个原始文件只保留最近 keep_latest 份，且只清理超过
    older_than_days 天的（保证短期内出问题还能恢复，不会一清理就全没了）。
    """
    import time as _time

    groups: Dict[str, List[str]] = {}
    for p in _iter_known_backup_files():
        original = _BAK_SUFFIX_RE.sub("", p)
        groups.setdefault(original, []).append(p)

    cutoff = _time.time() - max(0, older_than_days) * 86400
    removed: List[str] = []
    errors: List[str] = []
    for _original, paths in groups.items():
        stat_paths = []
        for p in paths:
            try:
                stat_paths.append((os.stat(p).st_mtime, p))
            except Exception:  # noqa: BLE001
                continue
        stat_paths.sort(key=lambda x: -x[0])
        for i, (mtime, p) in enumerate(stat_paths):
            if i < max(0, keep_latest):
                continue
            if mtime > cutoff:
                continue
            try:
                os.remove(p)
                removed.append(p)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p}: {e}")

    return {
        "success": True,
        "removed_count": len(removed),
        "removed": removed[:50],
        "errors": errors[:20],
        "message": f"已清理 {len(removed)} 个旧备份" + (f"，{len(errors)} 个失败" if errors else ""),
    }


def list_all_nonsteam_games() -> List[Dict[str, Any]]:
    """解析所有用户 shortcuts.vdf，列出非 Steam 游戏。"""
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


def start_dir_shared_with_others(start: str, appid: Any) -> List[Dict[str, Any]]:
    """StartDir 是否被其它快捷方式共用（共用则不能整目录当本体删）。"""
    start_n = _normalize(start) or ""
    if not start_n:
        return []
    target = normalize_appid(appid)
    shared: List[Dict[str, Any]] = []
    start_slash = start_n.rstrip("/") + "/"
    for g in list_all_nonsteam_games():
        if normalize_appid(g.get("appid")) == target:
            continue
        other_start = _normalize(g.get("start_dir") or "") or ""
        other_exe = _normalize(g.get("exe") or "") or ""
        hit = False
        if other_start and (other_start == start_n or other_start.startswith(start_slash)):
            hit = True
        if other_exe and other_exe.startswith(start_slash):
            hit = True
        if hit:
            shared.append(
                {
                    "appid": normalize_appid(g.get("appid")),
                    "name": g.get("name") or "",
                    "exe": g.get("exe") or "",
                }
            )
    return shared


def find_duplicate_nonsteam_groups(games: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """按 exe / 同名找出重复快捷方式。"""
    if games is None:
        games = list_all_nonsteam_games()
    by_exe: Dict[str, List[Dict[str, Any]]] = {}
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for g in games:
        exe = _normalize(g.get("exe") or "") or ""
        name = str(g.get("name") or "").strip().lower()
        if exe:
            by_exe.setdefault(exe, []).append(g)
        if name:
            by_name.setdefault(name, []).append(g)

    groups: List[Dict[str, Any]] = []
    seen_idsets = set()
    for exe, items in by_exe.items():
        if len(items) < 2:
            continue
        idset = tuple(sorted(normalize_appid(i.get("appid")) for i in items))
        seen_idsets.add(idset)
        groups.append(
            {
                "reason": "same_exe",
                "label": os.path.basename(exe) or exe,
                "exe": exe,
                "games": items,
            }
        )
    for name, items in by_name.items():
        if len(items) < 2:
            continue
        idset = tuple(sorted(normalize_appid(i.get("appid")) for i in items))
        if idset in seen_idsets:
            continue
        groups.append(
            {
                "reason": "same_name",
                "label": items[0].get("name") or name,
                "exe": "",
                "games": items,
            }
        )
    return {
        "success": True,
        "groups": groups,
        "count": len(groups),
        "dup_entry_count": sum(len(g["games"]) for g in groups),
        "message": f"发现 {len(groups)} 组重复快捷方式" if groups else "未发现重复快捷方式",
    }


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
    r"payload|installer|crash_reporter|helper\.exe|"
    r"inject(?:or)?|mtool|getpefileinfo|createdump|nwjc|"
    r"bandizip|winrar|winzip|7zfm|7zg|"
    r"patcher|workshopuploader|steamworkshop|"
    r"oalinst|vc_redist|dxwebsetup|steamclient_loader|"
    r"configtool|dbghelp|crashrpt|unitycrash|"
    r"savedata|save.?data|unins000|unitycrashhandler|"
    r"crashpad_handler|notification_helper)",
    re.I,
)
_SKIP_NAME_RE = re.compile(
    r"^(inject|mtool|createdump|oalinst|unins\d*|setup|install|"
    r"handler|getpefileinfo|nwjc|savedata)$",
    re.I,
)
_SKIP_DIR_NAMES = {
    "_commonredist",
    "redist",
    "_redist",
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
    "loaders",
    "解压工具",
    "汉化补丁",
    "日文原版文件备份",
    "steamworkshopuploader",
    "_exe_extract",
    "savedata",
    "save",
    "_backup",
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
        # 截图设为图标时的输出最长边（像素）；0=原图不缩放
        "screenshot_max_edge": 768,
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
    try:
        sme = int(defaults.get("screenshot_max_edge", 768) or 0)
        # 0=原图；否则限制在合理范围
        if sme < 0:
            sme = 0
        elif sme > 0:
            sme = max(128, min(2048, sme))
        defaults["screenshot_max_edge"] = sme
    except Exception:  # noqa: BLE001
        defaults["screenshot_max_edge"] = 768
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
    if "screenshot_max_edge" in settings:
        try:
            sme = int(settings.get("screenshot_max_edge") or 0)
            if sme < 0:
                sme = 0
            elif sme > 0:
                sme = max(128, min(2048, sme))
            cur["screenshot_max_edge"] = sme
        except Exception:  # noqa: BLE001
            pass
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
    stem = os.path.splitext(name)[0]
    if _SKIP_NAME_RE.search(stem):
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
    if "updater" in lower or ("launcher" in lower and "game" not in lower):
        score -= 15
    if "language" in lower or "config" in lower or "setting" in lower:
        score -= 40
    if "crash" in lower or "report" in lower:
        score -= 100
    if any(x in lower for x in ("inject", "mtool", "patch", "setup", "install", "unpack")):
        score -= 80
    return score


# 技术/中间目录：命名时跳过，继续向上找「游戏文件夹」
_TECH_FOLDER_NAMES = {
    "bin",
    "binaries",
    "win64",
    "win32",
    "x64",
    "x86",
    "x86_64",
    "game",
    "games",
    "engine",
    "redist",
    "_commonredist",
    "support",
    "data",
    "datas",
    "resources",
    "content",
    "plugins",
    "modules",
    "system",
    "ship",
    "shipping",
    "windows",
    "win",
    "linux",
    "macos",
    "mac",
    "program",
    "program files",
    "program files (x86)",
    "steamapps",
    "common",
    "launcher",
    "launchers",
    "tools",
    "bin_win64",
    "bin_win32",
    "x64shipping",
    "development",
    "debug",
    "release",
}


def _is_pack_or_tech_folder(name: str) -> bool:
    """打包码 / 纯数字 / 技术目录，不适合作为游戏显示名。"""
    if not name or name in (".", ".."):
        return True
    n = name.lower().strip()
    if n in _TECH_FOLDER_NAMES:
        return True
    if re.fullmatch(r"xj\d+", n):
        return True
    if re.fullmatch(r"\d+", n):
        return True
    # 过短的无意义名
    if len(n) <= 1:
        return True
    return False


# 有问题的游戏：文件夹名后缀标记（不必删除，便于识别/跳过自动添加）
TROUBLE_SUFFIX = "-trouble"


def _strip_trouble_suffix(name: str) -> str:
    n = str(name or "")
    if n.lower().endswith(TROUBLE_SUFFIX):
        return n[: -len(TROUBLE_SUFFIX)]
    return n


def _has_trouble_suffix(name: str) -> bool:
    return str(name or "").lower().endswith(TROUBLE_SUFFIX)


def _path_has_trouble(path: str) -> bool:
    """路径任一层文件夹名以 -trouble 结尾则视为问题项。"""
    p = _normalize(path) or str(path or "")
    if not p:
        return False
    for part in p.replace("\\", "/").split("/"):
        if part and _has_trouble_suffix(part):
            return True
    return False


def _collect_path_parts(exe_path: str, scan_root: str = "") -> List[str]:
    """从 exe 目录向上到 scan_root（不含）收集路径段，靠近 root 在前。"""
    exe_path = _normalize(exe_path) or exe_path
    if not exe_path:
        return []
    scan_root_rp = ""
    if scan_root:
        try:
            scan_n = _normalize(scan_root) or os.path.expanduser(str(scan_root))
            scan_root_rp = os.path.realpath(scan_n)
        except Exception:  # noqa: BLE001
            scan_root_rp = ""

    cur = os.path.dirname(os.path.realpath(exe_path))
    parts: List[str] = []
    for _ in range(12):
        if not cur or cur in ("/", "."):
            break
        try:
            cur_rp = os.path.realpath(cur)
        except Exception:  # noqa: BLE001
            cur_rp = cur
        if scan_root_rp and cur_rp == scan_root_rp:
            break
        base = os.path.basename(cur_rp.rstrip("/"))
        if base:
            parts.append(base)
        parent = os.path.dirname(cur_rp)
        if parent == cur_rp:
            break
        cur = parent
    parts.reverse()
    return parts


_TROUBLE_CONTAINERS = {
    "installed",
    "games",
    "game",
    "pc",
    "roms",
    "common",
    "bin",
    "binaries",
    "win64",
    "win32",
    "windows",
    "linux",
    "downloads",
}


def _scan_ceiling_for_exe(exe_path: str, scan_root: str = "") -> str:
    """只认真正的扫描根（Downloads 等），绝不把 StartDir 当上限。"""
    exe_n = _normalize(exe_path) or ""
    if not exe_n:
        return ""
    settings = load_settings()
    # 注意：不要把传入的 start_dir 放进候选
    candidates = [
        settings.get("scan_path") or "",
        _DEFAULT_SCAN_PATH,
        os.path.expanduser("~/Downloads"),
        os.path.expanduser("~/Games"),
        "/home/deck/Downloads",
    ]
    hint = _normalize(str(scan_root or "")) or ""
    # 只有「看起来像扫描根」的 hint 才用（浅、且不是 exe 所在目录）
    if hint and os.path.isdir(hint):
        exe_dir = os.path.dirname(exe_n)
        hint_base = os.path.basename(hint.rstrip("/")).lower()
        if hint != exe_dir and hint_base not in _TROUBLE_CONTAINERS and not hint_base.isdigit():
            rel_ok = exe_n.startswith(hint.rstrip("/") + "/")
            # hint 必须是 Downloads / Games 本身，或与 settings 相同
            allowed_hints = {_normalize(c) for c in candidates if c}
            if hint in allowed_hints and rel_ok:
                candidates.insert(0, hint)

    exe_dir = os.path.dirname(exe_n)
    usable: List[str] = []
    for raw in candidates:
        c = _normalize(str(raw or "")) or ""
        if not c or not os.path.isdir(c):
            continue
        if c == exe_dir:
            continue
        if exe_n == c or exe_n.startswith(c.rstrip("/") + "/"):
            rel = os.path.relpath(exe_n, c)
            depth = 0 if rel == "." else rel.count(os.sep)
            if depth >= 1:
                usable.append(c)
    if not usable:
        return ""
    # 在允许的扫描根里选最长的（Downloads 优于 /home/deck）
    usable = sorted(set(usable), key=len, reverse=True)
    return usable[0]


def _resolve_game_folder(exe_path: str, scan_root: str = "", name: str = "") -> str:
    """解析用于 -trouble 标记的最上级「名称」文件夹。

    例如：
      名称 A1051，exe 在 Downloads/A1051/game/A1051/01/08/xxx.exe
        → Downloads/A1051
      而不是深层的 .../game/A1051 或 01/08。
    """
    exe_path = _normalize(exe_path) or exe_path
    if not exe_path:
        return ""
    try:
        exe_rp = os.path.realpath(exe_path)
    except Exception:  # noqa: BLE001
        exe_rp = exe_path
    start = os.path.dirname(exe_rp)

    ceiling = _scan_ceiling_for_exe(exe_rp, scan_root)
    want = _strip_trouble_suffix(str(name or "").strip())

    # 从 exe 目录向上收集到 ceiling（不含）
    chain: List[str] = []
    cur = start
    for _ in range(16):
        if not cur or cur in ("/", "."):
            break
        try:
            cur_rp = os.path.realpath(cur)
        except Exception:  # noqa: BLE001
            cur_rp = cur
        if ceiling and cur_rp == os.path.realpath(ceiling):
            break
        chain.append(cur_rp)
        parent = os.path.dirname(cur_rp)
        if parent == cur_rp:
            break
        cur = parent
    if not chain:
        return start

    # 靠近扫描根的在前：Downloads/A1051 优先于 .../game/A1051
    chain_top_first = list(reversed(chain))

    def _base(p: str) -> str:
        return os.path.basename(p.rstrip("/"))

    def _name_hit(p: str) -> bool:
        if not want:
            return False
        b = _strip_trouble_suffix(_base(p))
        bl = b.lower()
        wl = want.lower()
        if bl == wl:
            return True
        # 文件夹名以游戏名开头也算（带版本号时）
        if len(wl) >= 3 and (bl.startswith(wl) or wl.startswith(bl)):
            return True
        return False

    def _is_container(p: str) -> bool:
        b = _strip_trouble_suffix(_base(p)).lower()
        return b in _TROUBLE_CONTAINERS or _is_pack_or_tech_folder(b) or b.isdigit()

    # 1) 名称匹配：取最靠近扫描根的那一层（Downloads/A1166）
    for p in chain_top_first:
        if _name_hit(p) and os.path.isdir(p) and not _is_container(p):
            return p

    # 2) 扫描根下第一层非容器目录；若第一层是 installed/Games，再往下一层
    if ceiling:
        try:
            ceil_rp = os.path.realpath(ceiling)
        except Exception:  # noqa: BLE001
            ceil_rp = ceiling
        for p in chain_top_first:
            try:
                parent = os.path.dirname(os.path.realpath(p))
            except Exception:  # noqa: BLE001
                parent = os.path.dirname(p)
            if parent != ceil_rp:
                continue
            if not _is_container(p) and os.path.isdir(p):
                return p
            # Downloads/installed/Foo → 继续找 Foo
            for q in chain_top_first:
                try:
                    if os.path.dirname(os.path.realpath(q)) == os.path.realpath(p) and not _is_container(q):
                        return q
                except Exception:  # noqa: BLE001
                    continue

    # 3) 最靠近根、且不是容器/技术/纯数字的一层
    for p in chain_top_first:
        if not _is_container(p) and os.path.isdir(p):
            return p

    return chain_top_first[0]


def _guess_game_name(exe_path: str, scan_root: str = "") -> str:
    """用「游戏所在文件夹名」作为显示名，不用 exe 文件名。

    例如：
      .../Drop Duchy v1.0.1/DropDuchy/Binaries/Win64/xxx.exe
        → 跳过 Win64、Binaries →「DropDuchy」
      .../XJ07980/DRAPLINE.Early.Access/DRAPLINE.exe
        → 跳过 XJ07980 →「DRAPLINE.Early.Access」
      .../SomeGame-trouble/game.exe
        →「SomeGame-trouble」（保留问题标记）
    仅在完全无法从路径取到文件夹时，才回退到 exe 主名。
    """
    exe_path = _normalize(exe_path) or exe_path
    if not exe_path:
        return "Game"

    parts = _collect_path_parts(exe_path, scan_root)
    meaningful = [p for p in parts if not _is_pack_or_tech_folder(p)]

    if meaningful:
        # 取最靠近启动器、且有意义的文件夹（游戏根目录）
        return meaningful[-1]

    # 若全被过滤：用相对 scan_root 的第一层目录
    if parts:
        for p in parts:
            if not _is_pack_or_tech_folder(p):
                return p
        return parts[-1] if parts else parts[0]

    # 最后回退：start 目录名，再不行才用 exe 主名
    parent = os.path.basename(os.path.dirname(os.path.realpath(exe_path)))
    if parent and not _is_pack_or_tech_folder(parent):
        return parent
    return os.path.splitext(os.path.basename(exe_path))[0]


def _rewrite_path_prefix(path: str, old_prefix: str, new_prefix: str) -> str:
    """把路径中 old_prefix 前缀换成 new_prefix（保留引号 / Z: 盘符风格）。"""
    raw = str(path or "")
    if not raw or not old_prefix:
        return raw
    quoted = False
    body = raw.strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        quoted = True
        body = body[1:-1]
    body_n = body.replace("\\", "/")
    old_n = old_prefix.replace("\\", "/").rstrip("/")
    new_n = new_prefix.replace("\\", "/").rstrip("/")

    def _swap(text: str, old: str, new: str) -> Optional[str]:
        if text == old or text.startswith(old + "/"):
            return new + text[len(old) :]
        if text.lower() == old.lower() or text.lower().startswith(old.lower() + "/"):
            return new + text[len(old) :]
        return None

    # 裸 Linux 路径
    swapped = _swap(body_n, old_n, new_n)
    # Proton/Steam 常见 Z:/home/deck/...
    if swapped is None and len(body_n) >= 2 and body_n[1] == ":":
        drive = body_n[:2]
        rest = body_n[2:]
        if not rest.startswith("/"):
            rest = "/" + rest
        inner = _swap(rest, old_n, new_n)
        if inner is not None:
            swapped = drive + inner
    if swapped is None:
        return raw
    return f'"{swapped}"' if quoted else swapped


def _apply_trouble_appname(name: str, mark: bool) -> str:
    """游戏显示名后加/去掉 -trouble。"""
    n = str(name or "").strip()
    if not n:
        return n
    if mark:
        return n if _has_trouble_suffix(n) else n + TROUBLE_SUFFIX
    return _strip_trouble_suffix(n)


def _update_shortcuts_after_folder_rename(
    old_folder: str,
    new_folder: str,
    mark: Optional[bool] = None,
) -> int:
    """重命名游戏目录后，同步所有用户 shortcuts 里的路径，并按需改 AppName。"""
    root = find_steam_root()
    if not root:
        return 0
    old_n = os.path.realpath(old_folder).replace("\\", "/").rstrip("/")
    new_n = os.path.realpath(new_folder).replace("\\", "/").rstrip("/")
    if not old_n or old_n == new_n:
        return 0
    ud = os.path.join(root, "userdata")
    if not os.path.isdir(ud):
        return 0
    entry_updated = 0
    for sid in os.listdir(ud):
        sc = os.path.join(ud, sid, "config", "shortcuts.vdf")
        if not os.path.isfile(sc):
            continue
        try:
            with open(sc, "rb") as fp:
                parsed = _read_node(fp)
        except Exception as e:  # noqa: BLE001
            logger.warning("read shortcuts for rename update failed %s: %s", sc, e)
            continue
        shortcuts = parsed.get("shortcuts")
        if not isinstance(shortcuts, dict):
            continue
        file_changed = False
        for _k, entry in shortcuts.items():
            if not isinstance(entry, dict):
                continue
            row_changed = False
            for field in ("Exe", "StartDir", "icon", "ShortcutPath", "LaunchOptions"):
                val = entry.get(field)
                if not isinstance(val, str) or not val:
                    continue
                new_val = _rewrite_path_prefix(val, old_n, new_n)
                if new_val != val:
                    entry[field] = new_val
                    row_changed = True
            if mark is not None:
                exe_now = str(entry.get("Exe") or "")
                under = False
                for probe in (exe_now, _normalize(exe_now) or ""):
                    if not probe:
                        continue
                    pn = probe.replace("\\", "/").strip('"')
                    if pn.startswith(old_n) or pn.startswith(new_n) or f":{old_n}" in pn or f":{new_n}" in pn:
                        under = True
                        break
                if under:
                    old_name = str(entry.get("AppName") or "")
                    new_name = _apply_trouble_appname(old_name, bool(mark))
                    if new_name and new_name != old_name:
                        entry["AppName"] = new_name
                        row_changed = True
            if row_changed:
                file_changed = True
                entry_updated += 1
        if file_changed:
            try:
                shutil.copy2(sc, sc + f".bak_nsc_mv_{int(__import__('time').time())}")
            except Exception:  # noqa: BLE001
                pass
            try:
                write_vdf(sc, parsed)
                logger.info(
                    "shortcuts path rewrite %s: %s -> %s (%s entries)",
                    sc,
                    old_n,
                    new_n,
                    entry_updated,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("write shortcuts after rename failed %s: %s", sc, e)
    return entry_updated


def _update_hidden_exes_after_rename(old_folder: str, new_folder: str) -> int:
    """同步设置里 hidden_exes 的路径前缀。"""
    cur = load_settings()
    hidden = list(cur.get("hidden_exes") or [])
    if not hidden:
        return 0
    old_n = os.path.realpath(old_folder).replace("\\", "/").rstrip("/")
    new_n = os.path.realpath(new_folder).replace("\\", "/").rstrip("/")
    changed = 0
    new_hidden = []
    for h in hidden:
        nh = _rewrite_path_prefix(str(h), old_n, new_n)
        if nh != h:
            changed += 1
        new_hidden.append(nh)
    if changed:
        cur["hidden_exes"] = new_hidden
        save_settings(cur)
    return changed


def _restore_nested_trouble_dirs(top_folder: str) -> List[str]:
    """把名称文件夹内部误加的 06-trouble 等改回原名，只改路径不改游戏名。"""
    top = os.path.realpath(top_folder) if top_folder else ""
    if not top or not os.path.isdir(top):
        return []
    restored: List[str] = []
    for dirpath, dirnames, _fns in os.walk(top, topdown=False):
        try:
            cur = os.path.realpath(dirpath)
        except Exception:  # noqa: BLE001
            cur = dirpath
        if cur == top:
            continue
        base = os.path.basename(cur.rstrip("/"))
        if not _has_trouble_suffix(base):
            continue
        new_path = os.path.join(os.path.dirname(cur), _strip_trouble_suffix(base))
        if os.path.exists(new_path):
            logger.warning("skip nested trouble restore, exists: %s", new_path)
            continue
        try:
            os.rename(cur, new_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("nested trouble restore failed %s: %s", cur, e)
            continue
        _update_shortcuts_after_folder_rename(cur, new_path, mark=None)
        _update_hidden_exes_after_rename(cur, new_path)
        restored.append(f"{cur} -> {new_path}")
        logger.info("restored nested trouble %s -> %s", cur, new_path)
    return restored


def mark_games_trouble(
    exes: List[str],
    scan_root: str = "",
    mark: bool = True,
    name: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """给最上级名称文件夹加/去掉 -trouble 后缀（不删除文件）。

    mark=True  →  A1051  → A1051-trouble
    mark=False →  A1051-trouble → A1051
    已在 Steam 的快捷方式 Exe/StartDir/icon 会一并改写。
    """
    settings = load_settings()
    raw_root = str(scan_root or settings.get("scan_path") or _DEFAULT_SCAN_PATH)
    root = _normalize(raw_root) or raw_root
    done = []
    skipped = []
    seen_folders = set()

    for raw in exes or []:
        exe = _normalize(str(raw)) or str(raw or "").strip()
        if not exe:
            skipped.append({"exe": raw, "reason": "空路径"})
            continue
        if not os.path.isfile(exe) and not os.path.isdir(os.path.dirname(exe)):
            # 文件可能刚被挪走；仍尝试从路径解析文件夹
            pass

        folder = _resolve_game_folder(exe, root, name=name)
        if not folder or not os.path.isdir(folder):
            # 回退：exe 所在目录
            folder = os.path.dirname(os.path.realpath(exe)) if os.path.exists(exe) else ""
        if not folder or not os.path.isdir(folder):
            skipped.append({"exe": exe, "reason": "找不到游戏文件夹"})
            continue

        try:
            folder_rp = os.path.realpath(folder)
        except Exception:  # noqa: BLE001
            folder_rp = folder

        # 禁止把 Downloads / home 整层改名
        shallow = {os.path.realpath(p) for p in list(_PROTECT_START_DIRS) + list(_PROTECT_BASE) if p}
        if folder_rp in shallow or folder_rp in (
            os.path.realpath(root) if root else "",
            os.path.expanduser("~/Downloads"),
            "/home/deck/Downloads",
        ):
            skipped.append({"exe": exe, "reason": "目标层过浅（扫描根/受保护目录）", "folder": folder_rp})
            continue
        parts = [x for x in folder_rp.strip("/").split("/") if x]
        if len(parts) < 3:
            skipped.append({"exe": exe, "reason": "路径过浅", "folder": folder_rp})
            continue

        if folder_rp in seen_folders:
            skipped.append({"exe": exe, "reason": "同文件夹已处理", "folder": folder_rp})
            continue

        nested_restored: List[str] = []
        if not dry_run:
            nested_restored = _restore_nested_trouble_dirs(folder_rp)

        base = os.path.basename(folder_rp.rstrip("/"))
        parent = os.path.dirname(folder_rp)
        if mark:
            if _has_trouble_suffix(base):
                if nested_restored:
                    done.append(
                        {
                            "exe": exe,
                            "old_folder": folder_rp,
                            "new_folder": folder_rp,
                            "name": base,
                            "shortcuts_updated": 0,
                            "restored_nested": nested_restored,
                            "marked": True,
                            "message": "顶层已是 -trouble，已纠正内部误标记",
                        }
                    )
                else:
                    skipped.append(
                        {
                            "exe": exe,
                            "reason": "已是 -trouble",
                            "folder": folder_rp,
                            "name": base,
                        }
                    )
                seen_folders.add(folder_rp)
                continue
            new_base = base + TROUBLE_SUFFIX
        else:
            if not _has_trouble_suffix(base):
                skipped.append(
                    {
                        "exe": exe,
                        "reason": "未标记 -trouble",
                        "folder": folder_rp,
                        "name": base,
                    }
                )
                seen_folders.add(folder_rp)
                continue
            new_base = _strip_trouble_suffix(base)

        new_folder = os.path.join(parent, new_base)
        if os.path.exists(new_folder):
            skipped.append(
                {
                    "exe": exe,
                    "reason": f"目标已存在: {new_base}",
                    "folder": folder_rp,
                }
            )
            continue

        if dry_run:
            done.append(
                {
                    "exe": exe,
                    "new_exe": _rewrite_path_prefix(exe, folder_rp, new_folder),
                    "old_folder": folder_rp,
                    "new_folder": new_folder,
                    "name": new_base,
                    "shortcuts_updated": 0,
                    "hidden_updated": 0,
                    "marked": mark,
                    "dry_run": True,
                    "new_appname": _apply_trouble_appname(name or base, mark),
                    "steam_running": is_steam_running(),
                }
            )
            seen_folders.add(folder_rp)
            continue

        try:
            os.rename(folder_rp, new_folder)
        except Exception as e:  # noqa: BLE001
            logger.error("rename trouble mark failed %s -> %s: %s", folder_rp, new_folder, e)
            skipped.append({"exe": exe, "reason": f"重命名失败: {e}", "folder": folder_rp})
            continue

        seen_folders.add(os.path.realpath(new_folder))
        sc_n = _update_shortcuts_after_folder_rename(folder_rp, new_folder, mark=mark)
        hid_n = _update_hidden_exes_after_rename(folder_rp, new_folder)
        # 新 exe 路径（若原 exe 在该文件夹下）
        new_exe = _rewrite_path_prefix(exe, folder_rp, new_folder)
        done.append(
            {
                "exe": exe,
                "new_exe": new_exe,
                "old_folder": folder_rp,
                "new_folder": os.path.realpath(new_folder),
                "name": new_base,
                "shortcuts_updated": sc_n,
                "hidden_updated": hid_n,
                "marked": mark,
                "restored_nested": nested_restored,
                "new_appname": _apply_trouble_appname(name or new_base, mark),
            }
        )
        logger.info(
            "trouble mark=%s %s -> %s (shortcuts=%s)",
            mark,
            folder_rp,
            new_folder,
            sc_n,
        )

    action = "标记 -trouble" if mark else "取消 -trouble"
    return {
        "success": True,
        "marked": mark,
        "done": done,
        "skipped": skipped,
        "done_count": len(done),
        "skipped_count": len(skipped),
        "steam_running": is_steam_running(),
        "message": (
            f"{action} 成功 {len(done)} 个，跳过 {len(skipped)} 个"
            "（最上层文件夹 + 游戏名，已同步快捷方式路径）。"
            + (" Steam 正在运行，请完全退出后再打开。" if is_steam_running() else "")
        ),
    }


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

# 分卷压缩包：7z 原生分卷 (xxx.7z.001/.002/...)、RAR 新式分卷 (xxx.part1.rar/.part2.rar/...)。
# 只有「第一卷」需要真正扔给解压工具处理（7z/unrar 会自动找同目录下的后续卷），
# 其余卷单独按压缩包处理只会报错，属于噪音，直接忽略。
_SPLIT_7Z_RE = re.compile(r"^(.+)\.7z\.(\d{3,})$", re.I)
_SPLIT_RAR_PART_RE = re.compile(r"^(.+)\.part(\d+)\.rar$", re.I)


def _archive_kind(name: str) -> str:
    """返回 'entry'(应直接扔给解压工具)/'part'(分卷附属卷，忽略)/''(不是压缩包)。"""
    m = _SPLIT_7Z_RE.match(name)
    if m:
        return "entry" if int(m.group(2)) == 1 else "part"
    m = _SPLIT_RAR_PART_RE.match(name)
    if m:
        return "entry" if int(m.group(2)) == 1 else "part"
    if _is_archive_file(name):
        return "entry"
    return ""


def _is_archive_file(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _ARCHIVE_EXTS)


def _archive_stem(name: str) -> str:
    m = _SPLIT_7Z_RE.match(name)
    if m:
        return m.group(1)
    m = _SPLIT_RAR_PART_RE.match(name)
    if m:
        return m.group(1)
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


# Linux 原生启动项需要可执行位才能被 Steam 直接 exec()；.exe 是通过 Proton 读取运行的，
# 不受此限制。压缩包（尤其是在 Windows 上打包的）通常不保留 unix 可执行位，
# 纯 Python zipfile 解压更是完全不还原权限位，导致解压出来的启动器加进 Steam 后无法启动。
_LINUX_LAUNCHER_EXTS = (".sh", ".x86_64", ".x86", ".appimage")


def _ensure_executable(path: str) -> None:
    """确保 Linux 原生启动项带有可执行位；静默失败（只读文件系统等）不影响扫描。"""
    try:
        st = os.stat(path)
        mode = st.st_mode
        want = mode | stat.S_IXUSR
        if mode & stat.S_IRGRP:
            want |= stat.S_IXGRP
        if mode & stat.S_IROTH:
            want |= stat.S_IXOTH
        if want != mode:
            os.chmod(path, want)
    except Exception:  # noqa: BLE001
        pass


def _clean_subprocess_env() -> dict:
    """去掉 Decky/PyInstaller 注入的库路径，避免系统 spectacle/ffmpeg 链错 so。"""
    env = os.environ.copy()
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    return env


def _run_cmd(cmd: List[str], timeout: int = 600, env: Optional[dict] = None) -> tuple:
    import subprocess

    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env if env is not None else _clean_subprocess_env(),
        )
        return r.returncode, (r.stderr or b"").decode("utf-8", "replace")[:500]
    except Exception as e:  # noqa: BLE001
        return 99, str(e)


def _archive_member_safe(name: str, dest_dir: str) -> bool:
    """拒绝压缩包内的绝对路径 / .. 穿越。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    # Windows 盘符
    if len(name) >= 2 and name[1] == ":":
        return False
    dest_rp = os.path.realpath(dest_dir)
    # zip 里可能是 dir/../etc/passwd
    joined = os.path.realpath(os.path.join(dest_dir, name.replace("\\", "/")))
    try:
        return os.path.commonpath([dest_rp, joined]) == dest_rp
    except ValueError:
        return False


def _safe_zip_extract(archive_path: str, dest_dir: str) -> None:
    import zipfile

    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if not _archive_member_safe(name, dest_dir):
                logger.warning("skip unsafe zip member %s in %s", name, archive_path)
                continue
            zf.extract(info, dest_dir)


def _safe_tar_extract(archive_path: str, dest_dir: str) -> None:
    import tarfile

    with tarfile.open(archive_path, "r:*") as tf:
        safe = []
        for member in tf.getmembers():
            if not _archive_member_safe(member.name, dest_dir):
                logger.warning("skip unsafe tar member %s in %s", member.name, archive_path)
                continue
            safe.append(member)
        tf.extractall(dest_dir, members=safe)


# 解压成功后在 dest_dir 内写入的标记文件名。仅凭目录是否非空判断"已解压"会把
# 半途失败（磁盘满/加密包/损坏包）留下的残余文件误当成功，此后每次扫描都静默跳过、
# 永远不会重试；改用显式标记后，只有真正解压成功过的目录才会被跳过。
_EXTRACT_OK_MARKER = ".nsc_extract_ok"


def _mark_extracted_ok(dest_dir: str) -> None:
    try:
        with open(os.path.join(dest_dir, _EXTRACT_OK_MARKER), "w", encoding="utf-8") as fp:
            fp.write("1")
    except Exception:  # noqa: BLE001
        pass


def _already_extracted(dest_dir: str) -> bool:
    if os.path.isfile(os.path.join(dest_dir, _EXTRACT_OK_MARKER)):
        return True
    # 兼容旧版本遗留的解压结果（当时没有标记文件）：非空即视为已完成，
    # 并补写标记，避免下次仍要靠这条兼容路径判断。
    if _dir_nonempty(dest_dir):
        _mark_extracted_ok(dest_dir)
        return True
    return False


def _extract_one_archive(archive_path: str, dest_dir: str) -> Dict[str, Any]:
    """解压单个压缩包到 dest_dir。优先 7z（兼容 zip/7z/rar/tar.*，含分卷）。"""
    os.makedirs(dest_dir, exist_ok=True)
    lower = archive_path.lower()
    if _already_extracted(dest_dir):
        return {"ok": True, "skipped": True, "dest": dest_dir, "message": "目标已存在，跳过"}

    # 1) 7z / 7za — 最通用，原生支持 7z/rar 分卷（给第一卷即可自动关联后续卷）
    for bin7 in ("7z", "7za"):
        if shutil.which(bin7):
            code, err = _run_cmd([bin7, "x", "-y", f"-o{dest_dir}", archive_path])
            if code == 0:
                _mark_extracted_ok(dest_dir)
                return {"ok": True, "skipped": False, "dest": dest_dir, "tool": bin7}
            logger.warning("7z extract fail %s: %s", archive_path, err)

    # 2) zip → unzip 或 zipfile
    if lower.endswith(".zip"):
        if shutil.which("unzip"):
            code, err = _run_cmd(["unzip", "-o", "-q", archive_path, "-d", dest_dir])
            if code == 0:
                _mark_extracted_ok(dest_dir)
                return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "unzip"}
        try:
            _safe_zip_extract(archive_path, dest_dir)
            _mark_extracted_ok(dest_dir)
            return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "zipfile"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "dest": dest_dir, "message": f"zip 解压失败: {e}"}

    # 3) rar（含 .partN.rar 分卷第一卷）→ unrar
    if lower.endswith(".rar") and shutil.which("unrar"):
        code, err = _run_cmd(["unrar", "x", "-o+", archive_path, dest_dir + "/"])
        if code == 0:
            _mark_extracted_ok(dest_dir)
            return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "unrar"}
        return {"ok": False, "dest": dest_dir, "message": err}

    # 4) tar 系列
    if any(lower.endswith(x) for x in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        if shutil.which("tar"):
            code, err = _run_cmd(["tar", "-xf", archive_path, "-C", dest_dir])
            if code == 0:
                _mark_extracted_ok(dest_dir)
                return {"ok": True, "skipped": False, "dest": dest_dir, "tool": "tar"}
        try:
            _safe_tar_extract(archive_path, dest_dir)
            _mark_extracted_ok(dest_dir)
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
                # 分卷压缩包（xxx.7z.002、xxx.part2.rar...）不单独处理，
                # 只处理第一卷，交给解压工具自动关联后续卷；否则每个附属卷
                # 都会被当成独立压缩包尝试解压、报一堆无意义的"失败"。
                if _archive_kind(fn) == "entry":
                    archives.append(os.path.join(dirpath, fn))

        if not archives:
            break

        level_did = 0
        for ap in archives:
            stem = _archive_stem(os.path.basename(ap))
            # 解压到同目录下的「去掉扩展名」文件夹
            dest = os.path.join(os.path.dirname(ap), stem)
            # 避免压缩包自身已经位于目标目录内部（例如经过软链接绕回）导致解压死循环
            ap_rp = os.path.realpath(ap)
            dest_rp = os.path.realpath(dest)
            if ap_rp == dest_rp or os.path.commonpath([ap_rp, dest_rp]) == dest_rp:
                continue
            # 已经成功解压过（标记文件存在，或兼容旧版本的非空目录）→ 跳过
            if _already_extracted(dest):
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


def _icons_data_dir() -> str:
    """图标稳定存放目录（添加后可后期手动替换同名文件）。"""
    env = (os.environ.get("DECKY_PLUGIN_SETTINGS_DIR") or "").strip()
    if env:
        # settings 旁的 data：.../settings/NonSteamCleaner -> .../data/NonSteamCleaner/icons
        # 直接用标准 homebrew 路径更清晰
        pass
    base = os.path.expanduser("~/homebrew/data/NonSteamCleaner/icons")
    os.makedirs(base, exist_ok=True)
    return base


# 图标文件名优先级（高 → 低）
_ICON_BASENAMES = (
    "icon.ico",
    "Icon.ico",
    "ICON.ICO",
    "game.ico",
    "Game.ico",
    "app.ico",
    "App.ico",
    "logo.ico",
    "icon.png",
    "Icon.png",
    "game.png",
    "Game.png",
    "logo.png",
    "Logo.png",
    "app.png",
    "AppIcon.png",
    "app_icon.png",
    "cover.png",
    "Cover.png",
)


def _score_icon_path(path: str, exe_stem: str) -> int:
    """图标匹配分数，越高越好。"""
    base = os.path.basename(path)
    lower = base.lower()
    stem = os.path.splitext(base)[0].lower()
    score = 0
    if lower.endswith(".ico"):
        score += 40
    elif lower.endswith(".png"):
        score += 30
    elif lower.endswith((".jpg", ".jpeg")):
        score += 15
    # 与 exe 同名
    if stem == exe_stem.lower():
        score += 100
    if stem in ("icon", "game", "app", "logo", "appicon", "app_icon"):
        score += 50
    # 排除杂项
    if any(x in lower for x in ("uninstall", "support", "gog", "steam", "crash", "sbicon", "webcache")):
        score -= 80
    try:
        sz = os.path.getsize(path)
        # 过小可能是占位图；过大也少见
        if 1024 <= sz <= 5 * 1024 * 1024:
            score += 10
        if sz < 200:
            score -= 50
    except Exception:  # noqa: BLE001
        pass
    return score


def _detect_image_ext(path: str) -> Optional[str]:
    """根据文件头判断 png/ico/jpg。"""
    try:
        with open(path, "rb") as fp:
            head = fp.read(16)
    except Exception:  # noqa: BLE001
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    # 有些 7z 抽出的大尺寸图标是 PNG 裸数据但无扩展名
    if len(head) >= 8 and head[0] == 0x89 and b"PNG" in head[:8]:
        return ".png"
    return None


def extract_icon_from_pe_exe(exe_path: str) -> Optional[str]:
    """从 Windows .exe 内嵌资源提取最佳图标（依赖 7z 读 PE/.rsrc）。

    返回提取后的本地文件路径（带正确扩展名），失败返回 None。
    """
    exe_path = _normalize(exe_path) or exe_path
    if not exe_path or not os.path.isfile(exe_path):
        return None
    if not exe_path.lower().endswith(".exe"):
        return None
    seven = shutil.which("7z") or shutil.which("7za")
    if not seven:
        logger.warning("7z 不可用，无法从 exe 提取图标")
        return None

    import hashlib
    import tempfile

    # 缓存：同一 exe 不重复解压
    h = hashlib.md5(exe_path.encode("utf-8", "replace")).hexdigest()[:12]
    try:
        st = os.stat(exe_path)
        cache_key = f"{h}_{int(st.st_mtime)}_{st.st_size}"
    except Exception:  # noqa: BLE001
        cache_key = h
    cache_dir = os.path.join(_icons_data_dir(), "_exe_extract", cache_key)
    cached_png = os.path.join(cache_dir, "best.png")
    cached_ico = os.path.join(cache_dir, "best.ico")
    if os.path.isfile(cached_png) and os.path.getsize(cached_png) > 100:
        return cached_png
    if os.path.isfile(cached_ico) and os.path.getsize(cached_ico) > 100:
        return cached_ico

    tmp = tempfile.mkdtemp(prefix="nsc_exe_icon_")
    try:
        # 只抽 ICON 资源，避免解出整个 .text
        patterns = [
            "-ir!.rsrc/*/ICON/*",
            "-ir!.rsrc/ICON/*",
            "-ir!*ICON*",
        ]
        extracted_any = False
        for pat in patterns:
            code, err = _run_cmd(
                [seven, "e", "-y", f"-o{tmp}", exe_path, pat],
                timeout=120,
            )
            # 即使部分失败，也可能已抽出文件
            try:
                if any(os.scandir(tmp)):
                    extracted_any = True
                    break
            except Exception:  # noqa: BLE001
                pass
            if code != 0:
                logger.debug("7z icon extract %s: %s", pat, err)

        if not extracted_any:
            return None

        # 挑选最大的有效图像
        best_path = None
        best_size = 0
        best_ext = None
        for dirpath, _dns, fns in os.walk(tmp):
            # 跳过 GROUP_ICON 一类极小文件
            if "GROUP_ICON" in dirpath.upper():
                continue
            for fn in fns:
                full = os.path.join(dirpath, fn)
                try:
                    sz = os.path.getsize(full)
                except Exception:  # noqa: BLE001
                    continue
                if sz < 200 or sz > 8 * 1024 * 1024:
                    continue
                ext = _detect_image_ext(full)
                if not ext:
                    # 无头但体积大的 ICON/1 常见为 PNG 裸流——再读一下
                    try:
                        with open(full, "rb") as fp:
                            b = fp.read(8)
                        if b.startswith(b"\x89PNG"):
                            ext = ".png"
                        else:
                            continue
                    except Exception:  # noqa: BLE001
                        continue
                # 优先更大尺寸（通常文件更大）
                rank = sz
                if ext == ".png":
                    rank += 50_000  # 略偏好 png
                if rank > best_size:
                    best_size = rank
                    best_path = full
                    best_ext = ext

        if not best_path or not best_ext:
            return None

        os.makedirs(cache_dir, exist_ok=True)
        dest = os.path.join(cache_dir, "best" + best_ext)
        shutil.copy2(best_path, dest)
        # 同步别名，方便后续查找
        if best_ext == ".png":
            try:
                shutil.copy2(dest, cached_png)
            except Exception:  # noqa: BLE001
                pass
        logger.info("extracted exe icon %s -> %s (%s bytes)", exe_path, dest, best_size)
        return dest
    except Exception as e:  # noqa: BLE001
        logger.warning("extract_icon_from_pe_exe failed %s: %s", exe_path, e)
        return None
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def find_icon_for_exe(exe_path: str, start_dir: str = "") -> Optional[str]:
    """在启动器附近查找图标；若无外置图标则从 .exe 内嵌资源提取。"""
    exe_path = _normalize(exe_path) or exe_path
    if not exe_path:
        return None
    exe_dir = os.path.dirname(exe_path)
    start = _normalize(start_dir) or exe_dir
    exe_stem = os.path.splitext(os.path.basename(exe_path))[0]

    search_dirs = []
    for d in (exe_dir, start, os.path.dirname(exe_dir), os.path.dirname(start)):
        if d and os.path.isdir(d) and d not in search_dirs:
            search_dirs.append(d)

    candidates: List[str] = []
    # 1) 外置图标文件
    for d in search_dirs:
        for ext in (".ico", ".png", ".jpg", ".jpeg"):
            p = os.path.join(d, exe_stem + ext)
            if os.path.isfile(p):
                candidates.append(p)
        for bn in _ICON_BASENAMES:
            p = os.path.join(d, bn)
            if os.path.isfile(p):
                candidates.append(p)
        try:
            for fn in os.listdir(d):
                lower = fn.lower()
                full = os.path.join(d, fn)
                if not os.path.isfile(full):
                    continue
                if lower.endswith(".ico"):
                    candidates.append(full)
                elif lower.endswith(".png") and (
                    "icon" in lower or "logo" in lower or "cover" in lower
                ):
                    candidates.append(full)
        except Exception:  # noqa: BLE001
            pass

    uniq = []
    seen = set()
    for c in candidates:
        try:
            rp = os.path.realpath(c)
        except Exception:  # noqa: BLE001
            rp = c
        if rp not in seen and os.path.isfile(rp):
            seen.add(rp)
            uniq.append(rp)

    if uniq:
        uniq.sort(key=lambda p: (-_score_icon_path(p, exe_stem), len(p)))
        best = uniq[0]
        if _score_icon_path(best, exe_stem) >= 0:
            return best

    # 2) 从 exe 内嵌资源提取（用户说 exe 自带图标）
    if exe_path.lower().endswith(".exe"):
        pe_icon = extract_icon_from_pe_exe(exe_path)
        if pe_icon:
            return pe_icon

    return None


def load_icon_data_url(icon_path: str, max_edge: int = 64) -> str:
    """把本地图标转成 data URL，供前端 <img> 直接显示（避免 file:// 被 CEF 拦截）。

    大图用 ffmpeg 缩到 max_edge 以控制体积。
    """
    import base64
    import tempfile

    icon_path = _normalize(icon_path) or icon_path
    if not icon_path or not os.path.isfile(icon_path):
        return ""

    ext = _detect_image_ext(icon_path) or os.path.splitext(icon_path)[1].lower() or ".png"
    mime = {
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(ext, "image/png")

    try:
        sz = os.path.getsize(icon_path)
    except Exception:  # noqa: BLE001
        return ""

    raw: Optional[bytes] = None
    # 小文件直接嵌入
    if sz <= 48 * 1024 and ext in (".png", ".jpg", ".jpeg", ".ico"):
        try:
            raw = open(icon_path, "rb").read()
        except Exception:  # noqa: BLE001
            raw = None

    # 大图 / 需要统一尺寸：ffmpeg 缩略图
    if raw is None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            tmp_out = None
            try:
                fd, tmp_out = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                # 保持比例，限制在 max_edge 内
                vf = f"scale={max_edge}:{max_edge}:force_original_aspect_ratio=decrease"
                code, err = _run_cmd(
                    [
                        ffmpeg,
                        "-y",
                        "-loglevel",
                        "error",
                        "-i",
                        icon_path,
                        "-vf",
                        vf,
                        tmp_out,
                    ],
                    timeout=30,
                )
                if code == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 50:
                    raw = open(tmp_out, "rb").read()
                    mime = "image/png"
                else:
                    logger.debug("ffmpeg thumb fail: %s", err)
            except Exception as e:  # noqa: BLE001
                logger.debug("ffmpeg thumb error: %s", e)
            finally:
                if tmp_out and os.path.exists(tmp_out):
                    try:
                        os.remove(tmp_out)
                    except Exception:  # noqa: BLE001
                        pass

    # 仍失败则硬塞（限制 150KB）
    if raw is None and sz <= 150 * 1024:
        try:
            raw = open(icon_path, "rb").read()
        except Exception:  # noqa: BLE001
            return ""

    if not raw:
        return ""
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _ffmpeg_to_png(src: str, dest: str, max_edge: int = 0) -> bool:
    """将任意图标转为 PNG；max_edge>0 时等比缩放到边长内（不放大）。"""
    return _ffmpeg_process_image(src, dest, max_edge=max_edge)


def _read_image_size(path: str) -> tuple:
    """读取宽高，失败返回 (0, 0)。"""
    try:
        with open(path, "rb") as fp:
            head = fp.read(24)
            if head.startswith(b"\x89PNG") and len(head) >= 24:
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return (0, 0)
            fp.seek(2)
            while True:
                marker = fp.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    break
                typ = marker[1]
                ln_b = fp.read(2)
                if len(ln_b) < 2:
                    break
                ln = struct.unpack(">H", ln_b)[0]
                if typ in (0xC0, 0xC1, 0xC2, 0xC3):
                    data = fp.read(max(0, ln - 2))
                    if len(data) >= 5:
                        h, w = struct.unpack(">HH", data[1:5])
                        return (int(w), int(h))
                    break
                fp.seek(max(0, ln - 2), os.SEEK_CUR)
    except Exception:  # noqa: BLE001
        return (0, 0)
    return (0, 0)


# 裁剪比例：Steam 库常用尺寸
CROP_PRESETS: Dict[str, Optional[tuple]] = {
    "none": None,  # 不裁，只缩放
    "square": (1, 1),
    "portrait": (2, 3),  # Deck 库竖版
    "capsule": (92, 43),  # Steam 胶囊 460x215
    "wide": (16, 9),
    "hero": (96, 31),  # 详情页横幅约 1920x620
}


def _compute_crop_box(
    width: int,
    height: int,
    mode: str = "none",
    align: str = "center",
) -> Optional[tuple]:
    """返回 (cw, ch, x, y)；None 表示不裁。"""
    if width <= 0 or height <= 0:
        return None
    key = str(mode or "none").strip().lower()
    if key not in CROP_PRESETS:
        key = "none"
    ratio = CROP_PRESETS[key]
    if not ratio:
        return None
    rw, rh = ratio
    if width * rh >= height * rw:
        ch = height
        cw = max(1, int(height * rw / rh))
    else:
        cw = width
        ch = max(1, int(width * rh / rw))
    cw = max(1, min(width, cw))
    ch = max(1, min(height, ch))
    al = str(align or "center").strip().lower()
    if al == "left":
        x, y = 0, (height - ch) // 2
    elif al == "right":
        x, y = width - cw, (height - ch) // 2
    elif al == "top":
        x, y = (width - cw) // 2, 0
    elif al == "bottom":
        x, y = (width - cw) // 2, height - ch
    else:
        x, y = (width - cw) // 2, (height - ch) // 2
    return (cw, ch, max(0, x), max(0, y))


def _ffmpeg_process_image(
    src: str,
    dest: str,
    max_edge: int = 0,
    crop_box: Optional[tuple] = None,
) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not src or not os.path.isfile(src):
        return False
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        vf: List[str] = []
        if crop_box:
            cw, ch, x, y = [int(v) for v in crop_box]
            if cw > 0 and ch > 0:
                vf.append(f"crop={cw}:{ch}:{x}:{y}")
        if max_edge and max_edge > 0:
            vf.append(
                f"scale='min({int(max_edge)},iw)':'min({int(max_edge)},ih)'"
                f":force_original_aspect_ratio=decrease:flags=lanczos"
            )
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", src]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd.append(dest)
        code, err = _run_cmd(cmd, timeout=45)
        if code == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 50:
            return True
        logger.debug("ffmpeg_process fail: %s", err)
    except Exception as e:  # noqa: BLE001
        logger.debug("ffmpeg_process error: %s", e)
    return False


def crop_image_for_icon(
    src: str,
    dest: str = "",
    mode: str = "none",
    align: str = "center",
    max_edge: int = 0,
) -> Dict[str, Any]:
    """按比例裁剪并可选缩放，写出 PNG。"""
    src_n = _normalize(src) or str(src or "")
    if not src_n or not os.path.isfile(src_n):
        return {"success": False, "path": "", "message": f"图片不存在: {src}"}
    w, h = _read_image_size(src_n)
    box = _compute_crop_box(w, h, mode, align)
    if not dest:
        dest = os.path.join(
            _screenshots_data_dir(),
            f"crop_{int(__import__('time').time())}_{mode}_{align}.png",
        )
    ok = _ffmpeg_process_image(src_n, dest, max_edge=max_edge, crop_box=box)
    if not ok or not os.path.isfile(dest):
        return {"success": False, "path": "", "message": "裁剪失败（需要 ffmpeg）"}
    nw, nh = _read_image_size(dest)
    return {
        "success": True,
        "path": os.path.realpath(dest),
        "src_w": w,
        "src_h": h,
        "out_w": nw,
        "out_h": nh,
        "crop": box,
        "mode": mode,
        "align": align,
    }


def list_recent_screenshots(
    appid: Any = 0,
    userdata_id: str = "",
    limit: int = 16,
    max_age_sec: int = 0,
) -> Dict[str, Any]:
    """列出可选截图（含缩略图 data URL），新的在前。"""
    import time as _time

    now = _time.time()
    files = _iter_steam_screenshot_files(appid=appid, userdata_id=userdata_id)
    # 再扫一遍全局，避免 Steam 把非 Steam 截图存到别的 appid
    extra = _iter_steam_screenshot_files(appid=0, userdata_id=userdata_id)
    for f in extra:
        if f not in files:
            files.append(f)

    aid_tokens = set(_appid_dir_names(appid)) if appid else set()
    scored: List[tuple] = []
    for f in files:
        try:
            st = os.stat(f)
        except Exception:  # noqa: BLE001
            continue
        if max_age_sec and max_age_sec > 0 and (now - st.st_mtime) > max_age_sec:
            continue
        if not _image_usable(f, min_bytes=12000):
            continue
        in_app = 0
        if aid_tokens:
            norm = f.replace("\\", "/")
            if any(f"/{t}/" in norm or norm.endswith(f"/{t}") for t in aid_tokens):
                in_app = 1
        scored.append((in_app, st.st_mtime, st.st_size, f))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    items: List[Dict[str, Any]] = []
    for in_app, mtime, sz, path in scored[: max(1, min(24, int(limit or 16)))]:
        w, h = _read_image_size(path)
        thumb = load_icon_data_url(path, max_edge=80)
        items.append(
            {
                "id": path,
                "path": path,
                "name": os.path.basename(path),
                "mtime": int(mtime),
                "size": int(sz),
                "width": w,
                "height": h,
                "in_app": bool(in_app),
                "icon_data_url": thumb,
                "label": (
                    f"{os.path.basename(path)}"
                    + (f"  {w}x{h}" if w and h else "")
                    + ("  ·本游戏" if in_app else "")
                ),
            }
        )
    return {
        "success": True,
        "shots": items,
        "count": len(items),
        "crop_presets": [
            {"id": "none", "label": "不裁（整张缩放）"},
            {"id": "portrait", "label": "竖版 2:3"},
            {"id": "square", "label": "正方形 1:1"},
            {"id": "capsule", "label": "胶囊 460:215"},
            {"id": "wide", "label": "宽屏 16:9"},
            {"id": "hero", "label": "横幅 16:5"},
        ],
        "align_presets": [
            {"id": "center", "label": "居中"},
            {"id": "top", "label": "靠上"},
            {"id": "bottom", "label": "靠下"},
            {"id": "left", "label": "靠左"},
            {"id": "right", "label": "靠右"},
        ],
        "message": f"找到 {len(items)} 张可用截图" if items else "没有可用截图（请先按 Steam+R1）",
    }


def _normalize_screenshot_max_edge(value: Any, default: int = 768) -> int:
    """0=原图；否则 128..2048。"""
    try:
        v = int(value)
    except Exception:  # noqa: BLE001
        v = default
    if v < 0:
        return 0
    if v == 0:
        return 0
    return max(128, min(2048, v))


def _remove_grid_logo(appid: Any, userdata_id: str) -> bool:
    """删除 grid/{appid}_logo.png。

    Steam Deck 游戏详情页顶部「名称栏」规则：
      - 有 _logo.png → 显示 logo 图，不再显示 AppName 文字
      - 无 logo → 显示 shortcuts.vdf 里的 AppName

    以前误把 exe 小图标写成 _logo.png，会导致详情页名称栏只有图标、没有文字标题。
    """
    root = find_steam_root()
    if not root or not userdata_id:
        return False
    aid = str(normalize_appid(appid))
    path = os.path.join(root, "userdata", str(userdata_id), "config", "grid", f"{aid}_logo.png")
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        logger.info("removed grid logo (restore title text): %s", path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("remove grid logo failed %s: %s", path, e)
        return False


def prepare_steam_icon(
    icon_src: str,
    appid: int,
    userdata_id: str,
    name: str = "",
    icon_max_edge: int = 256,
    capsule_max_edge: int = 0,
) -> Dict[str, str]:
    """把图标写入 Steam 能识别的位置，使库内非 Steam 项显示图标。

    Steam Deck / 新客户端对非 Steam 游戏图标依赖：
      - shortcuts.vdf 的 icon 字段（属性页小图标）
      - grid/{appid}_icon.png  （库列表图标，最关键）
      - grid/{appid}.png       （封面/胶囊，可选）
      - grid/{appid}p.png      （竖版封面，可选）

    icon_max_edge / capsule_max_edge:
      - >0 时等比缩放，最长边不超过该值（且不放大）
      - 0 表示不强制缩放（尽量原图）

    注意：不要写入 grid/{appid}_logo.png！
    Steam Deck 进入游戏详情页时，有 logo 就只显示 logo 图，名称栏不会再显示 AppName 文字。

    appid 文件名用无符号 32 位十进制字符串。
    """
    out: Dict[str, str] = {
        "icon": "",
        "grid_icon": "",
        "grid_capsule": "",
        "source": icon_src or "",
        "logo_removed": False,
        "icon_max_edge": int(icon_max_edge or 0),
        "capsule_max_edge": int(capsule_max_edge or 0),
    }
    # 无论有无图标源，都清掉可能误写的 logo，保证详情页名称栏显示文字
    if userdata_id:
        out["logo_removed"] = _remove_grid_logo(appid, userdata_id)

    if not icon_src or not os.path.isfile(icon_src):
        return out

    aid = str(normalize_appid(appid))
    icons_dir = _icons_data_dir()

    # 统一转成 PNG（Steam 对 _icon.png 最稳；ico 作 icon 字段也可以）
    # 稳定图按 icon 边长预处理，避免 shortcuts icon 字段过大
    stable_edge = int(icon_max_edge or 0)
    if stable_edge <= 0:
        stable_edge = 256
    stable_png = os.path.join(icons_dir, f"{aid}.png")
    src_ext = (_detect_image_ext(icon_src) or os.path.splitext(icon_src)[1].lower() or "").lower()

    ok_png = _ffmpeg_to_png(icon_src, stable_png, max_edge=stable_edge)
    if not ok_png and src_ext == ".png":
        try:
            shutil.copy2(icon_src, stable_png)
            ok_png = os.path.isfile(stable_png)
        except Exception as e:  # noqa: BLE001
            logger.warning("copy png icon failed: %s", e)
    if not ok_png and src_ext in (".ico", ".jpg", ".jpeg", ".png"):
        # 回退：直接拷贝原文件到 data（至少 icon 字段有得用）
        try:
            fallback = os.path.join(icons_dir, f"{aid}{src_ext or '.ico'}")
            shutil.copy2(icon_src, fallback)
            out["icon"] = fallback
        except Exception as e:  # noqa: BLE001
            logger.warning("copy raw icon failed: %s", e)
            out["icon"] = os.path.realpath(icon_src)
    else:
        out["icon"] = stable_png if ok_png else (os.path.realpath(icon_src))

    # Steam grid 目录
    root = find_steam_root()
    if not root or not userdata_id:
        return out

    grid_dir = os.path.join(root, "userdata", str(userdata_id), "config", "grid")
    try:
        os.makedirs(grid_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("mkdir grid failed: %s", e)
        return out

    # 封面用原图缩放；列表图标用 stable 或原图
    src_full = icon_src
    src_for_icon = stable_png if ok_png and os.path.isfile(stable_png) else icon_src

    def _place(src: str, name: str, max_edge: int = 0) -> str:
        dest = os.path.join(grid_dir, name)
        if src.lower().endswith(".png") and max_edge <= 0:
            try:
                shutil.copy2(src, dest)
                if os.path.isfile(dest):
                    return dest
            except Exception:  # noqa: BLE001
                pass
        if _ffmpeg_to_png(src, dest, max_edge=max_edge):
            return dest
        try:
            shutil.copy2(src, dest)
            return dest if os.path.isfile(dest) else ""
        except Exception:  # noqa: BLE001
            return ""

    # 列表小图标
    i_edge = int(icon_max_edge or 0)
    if i_edge <= 0:
        i_edge = 256
    grid_icon = _place(src_for_icon, f"{aid}_icon.png", max_edge=i_edge)
    # 封面 / 竖版 / 详情页横幅
    c_edge = int(capsule_max_edge or 0)
    grid_cap = _place(src_full, f"{aid}.png", max_edge=c_edge)
    grid_p = _place(src_full, f"{aid}p.png", max_edge=c_edge)
    grid_hero = _place(src_full, f"{aid}_hero.png", max_edge=c_edge)
    # Steam 有时认 jpg；再写一份
    _place(src_full, f"{aid}.jpg", max_edge=c_edge)
    _place(src_full, f"{aid}p.jpg", max_edge=c_edge)
    # 有符号 appid 文件名（部分客户端只认这个）
    try:
        signed = str(appid_to_steam_int32(normalize_appid(appid)))
    except Exception:  # noqa: BLE001
        signed = ""
    if signed and signed != aid:
        _place(src_for_icon, f"{signed}_icon.png", max_edge=i_edge)
        _place(src_full, f"{signed}.png", max_edge=c_edge)
        _place(src_full, f"{signed}p.png", max_edge=c_edge)
        _place(src_full, f"{signed}_hero.png", max_edge=c_edge)
    # 绝不写 _logo.png（见上方说明）

    if grid_icon:
        out["grid_icon"] = grid_icon
    if grid_cap:
        out["grid_capsule"] = grid_cap
    if grid_p:
        out["grid_portrait"] = grid_p
    if grid_hero:
        out["grid_hero"] = grid_hero

    # shortcuts.vdf icon 字段：绝对路径，无引号（与现有成功写入的 DropDuchy 一致）
    if out.get("icon"):
        out["icon"] = os.path.realpath(out["icon"])
    elif grid_icon:
        out["icon"] = os.path.realpath(grid_icon)

    logger.info(
        "steam icon prepared appid=%s name=%s icon=%s grid_icon=%s capsule=%s "
        "icon_edge=%s cap_edge=%s logo_removed=%s",
        aid,
        (name or "")[:40],
        out.get("icon"),
        out.get("grid_icon"),
        out.get("grid_capsule"),
        i_edge,
        c_edge,
        out.get("logo_removed"),
    )
    return out


def _screenshots_data_dir() -> str:
    d = os.path.expanduser("~/homebrew/data/NonSteamCleaner/screenshots")
    os.makedirs(d, exist_ok=True)
    return d


def _appid_dir_names(appid: Any) -> List[str]:
    """Steam 截图目录可能用无符号或有符号十进制 appid。"""
    u = normalize_appid(appid)
    names = [str(u)]
    try:
        signed = appid_to_steam_int32(u)
        if str(signed) not in names:
            names.append(str(signed))
    except Exception:  # noqa: BLE001
        pass
    return names


def _iter_steam_screenshot_files(appid: Any = 0, userdata_id: str = "") -> List[str]:
    """收集可能属于该游戏的截图文件（不含 thumbnails）。"""
    root = find_steam_root()
    if not root:
        return []
    files: List[str] = []
    ud_root = os.path.join(root, "userdata")
    if not os.path.isdir(ud_root):
        return []

    sids = [str(userdata_id)] if userdata_id else []
    if not sids:
        try:
            sids = [d for d in os.listdir(ud_root) if d.isdigit()]
        except Exception:  # noqa: BLE001
            sids = []

    app_names = _appid_dir_names(appid) if appid else []
    exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

    def _collect_under(cdir: str, max_depth: int = 4) -> None:
        if not os.path.isdir(cdir):
            return
        try:
            for dirpath, dirnames, filenames in os.walk(cdir):
                rel = os.path.relpath(dirpath, cdir)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth > max_depth:
                    dirnames[:] = []
                    continue
                # 跳过缩略图
                dirnames[:] = [d for d in dirnames if d.lower() != "thumbnails"]
                if os.path.basename(dirpath).lower() == "thumbnails":
                    continue
                for fn in filenames:
                    if fn.lower().endswith(exts):
                        files.append(os.path.join(dirpath, fn))
        except Exception:  # noqa: BLE001
            return

    for sid in sids:
        base760 = os.path.join(ud_root, sid, "760")
        if not os.path.isdir(base760):
            continue
        if app_names:
            for an in app_names:
                _collect_under(os.path.join(base760, "remote", an, "screenshots"), 2)
                _collect_under(os.path.join(base760, an, "screenshots"), 2)
        _collect_under(os.path.join(base760, "screenshots"), 2)
        # Steam+R1 对非 Steam 游戏常写到其它 gameid 目录，必须扫整个 remote
        _collect_under(os.path.join(base760, "remote"), 3)

    # 用户常见截图目录 + 插件自截目录
    extra_roots = [
        _screenshots_data_dir(),
        os.path.expanduser("~/Pictures/Screenshots"),
        os.path.expanduser("~/Pictures"),
        os.path.expanduser("~/Desktop"),
    ]
    for er in extra_roots:
        if not os.path.isdir(er):
            continue
        try:
            for fn in os.listdir(er):
                if fn.lower().endswith(exts):
                    files.append(os.path.join(er, fn))
        except Exception:  # noqa: BLE001
            pass

    # 去重
    seen = set()
    out = []
    for f in files:
        try:
            rp = os.path.realpath(f)
        except Exception:  # noqa: BLE001
            rp = f
        if rp in seen or not os.path.isfile(rp):
            continue
        seen.add(rp)
        out.append(rp)
    return out


def find_latest_screenshot(
    appid: Any = 0,
    userdata_id: str = "",
    max_age_sec: int = 0,
    prefer_appid: bool = True,
) -> Optional[str]:
    """找最新截图。prefer_appid 时优先该 appid 目录下的图。"""
    import time as _time

    now = _time.time()
    files = _iter_steam_screenshot_files(appid=appid, userdata_id=userdata_id)
    if not files:
        return None

    scored: List[tuple] = []
    aid_tokens = set(_appid_dir_names(appid)) if appid else set()
    for f in files:
        try:
            st = os.stat(f)
        except Exception:  # noqa: BLE001
            continue
        if max_age_sec and max_age_sec > 0 and (now - st.st_mtime) > max_age_sec:
            continue
        # 拒绝黑屏/空图（游戏模式 ffmpeg 产物只有 3KB）
        if st.st_size < 12000 or not _image_usable(f, min_bytes=12000):
            continue
        norm = f.replace("\\", "/")
        in_app = 0
        if prefer_appid and aid_tokens:
            if any(f"/{t}/" in norm or norm.endswith(f"/{t}") for t in aid_tokens):
                in_app = 1
        age = now - st.st_mtime
        recency = 2 if age <= 1800 else (1 if age <= 86400 else 0)
        is_steam760 = 1 if "/760/" in norm else 0
        scored.append((in_app, recency, is_steam760, st.st_mtime, f))

    if not scored:
        return None
    # 本游戏目录 > 半小时内 > Steam 截图目录 > 时间
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    return scored[0][4]


def _image_usable(path: str, min_bytes: int = 12000) -> bool:
    """拒绝游戏模式 x11grab 抓到的纯黑/空图（1280x800 只有 3KB）。"""
    if not path or not os.path.isfile(path):
        return False
    try:
        sz = os.path.getsize(path)
    except Exception:  # noqa: BLE001
        return False
    if sz < int(min_bytes):
        return False
    try:
        with open(path, "rb") as fp:
            head = fp.read(12)
    except Exception:  # noqa: BLE001
        return False
    return bool(
        head.startswith(b"\x89PNG")
        or head[:2] == b"\xff\xd8"
        or head[:4] == b"RIFF"
        or head[:4] == b"GIF8"
    )


def _in_gamescope() -> bool:
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY"):
        return True
    try:
        envp = f"/run/user/{os.getuid()}/gamescope-environment"
        if os.path.isfile(envp):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def capture_display_screenshot(dest: str = "", delay_ms: int = 400) -> Dict[str, Any]:
    """截取当前屏幕。游戏模式下 x11grab 只会抓到黑屏，必须拒绝。"""
    if not dest:
        dest = os.path.join(
            _screenshots_data_dir(),
            f"capture_{int(__import__('time').time())}.png",
        )
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.isfile(dest):
        try:
            os.remove(dest)
        except Exception:  # noqa: BLE001
            pass

    gamescope = _in_gamescope()

    spectacle = shutil.which("spectacle")
    if spectacle:
        cmd = [spectacle, "-b", "-n", "-m", "-o", dest]
        if delay_ms and delay_ms > 0:
            cmd.extend(["-d", str(int(delay_ms))])
        code, err = _run_cmd(cmd, timeout=60)
        if code == 0 and _image_usable(dest):
            return {"success": True, "path": os.path.realpath(dest), "tool": "spectacle"}
        logger.warning("spectacle capture failed code=%s err=%s", code, err)

    grim = shutil.which("grim")
    if grim:
        env = _clean_subprocess_env()
        wd = os.environ.get("GAMESCOPE_WAYLAND_DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        if wd:
            env["WAYLAND_DISPLAY"] = wd
        gs = f"/run/user/{os.getuid()}/gamescope-0"
        if os.path.exists(gs):
            env["WAYLAND_DISPLAY"] = "gamescope-0"
        code, err = _run_cmd([grim, dest], timeout=20, env=env)
        if code == 0 and _image_usable(dest):
            return {"success": True, "path": os.path.realpath(dest), "tool": "grim"}
        logger.warning("grim capture failed: %s", err)

    # 游戏模式：ffmpeg 抓 :0 得到纯黑 1280x800（约 3KB），绝不能当成功
    if gamescope:
        logger.info("skip ffmpeg x11grab: gamescope session (would capture black frame)")
        return {
            "success": False,
            "path": "",
            "gamescope": True,
            "message": (
                "游戏模式无法直接截取游戏画面。请先按 Steam+R1（或 F12）截一张，"
                "再点「用最新截图设为图标」。然后完全退出 Steam（不是只退游戏）再打开。"
            ),
        }

    ffmpeg = shutil.which("ffmpeg")
    display = os.environ.get("DISPLAY") or ":0"
    if ffmpeg and display:
        cmd = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "x11grab",
            "-i",
            display,
            "-frames:v",
            "1",
            dest,
        ]
        code, err = _run_cmd(cmd, timeout=30)
        if code == 0 and _image_usable(dest):
            return {"success": True, "path": os.path.realpath(dest), "tool": "ffmpeg-x11"}
        logger.warning("ffmpeg capture failed or blank: %s", err)

    return {
        "success": False,
        "path": "",
        "message": (
            "无法截取屏幕。请在游戏中按 Steam+R1（或 F12）截图后，"
            "再点「用最新截图设为图标」。改完后请完全退出 Steam 再打开。"
        ),
    }


def _update_shortcut_icon_field(
    userdata_id: str,
    appid: Any,
    icon_path: str,
    key: str = "",
) -> bool:
    """把 shortcuts.vdf 里对应项的 icon 字段写成 icon_path。"""
    root = find_steam_root()
    if not root or not userdata_id or not icon_path:
        return False
    sc_path = os.path.join(root, "userdata", str(userdata_id), "config", "shortcuts.vdf")
    if not os.path.isfile(sc_path):
        return False
    target = normalize_appid(appid)
    try:
        with open(sc_path, "rb") as fp:
            parsed = _read_node(fp)
        shortcuts = parsed.get("shortcuts") or {}
        if not isinstance(shortcuts, dict):
            return False
        changed = False
        if key and str(key) in shortcuts and isinstance(shortcuts[str(key)], dict):
            shortcuts[str(key)]["icon"] = icon_path
            changed = True
        else:
            for _k, entry in shortcuts.items():
                if not isinstance(entry, dict):
                    continue
                if normalize_appid(entry.get("appid")) == target:
                    entry["icon"] = icon_path
                    changed = True
        if changed:
            write_vdf(sc_path, parsed)
        return changed
    except Exception as e:  # noqa: BLE001
        logger.warning("update shortcut icon failed: %s", e)
        return False


def set_game_icon_from_image(
    appid: Any,
    image_path: str,
    userdata_id: str = "",
    name: str = "",
    key: str = "",
    max_edge: Any = None,
    crop: str = "none",
    align: str = "center",
) -> Dict[str, Any]:
    """用一张图片（截图）作为该非 Steam 游戏的库图标/封面。

    max_edge: 封面最长边像素；0=原图。列表图标取 min(512, max_edge或256)。
    crop / align: 写入前先裁剪。
    """
    appid_u = normalize_appid(appid)
    if not appid_u:
        return {"success": False, "message": "无效 appid"}
    img = _normalize(image_path) or str(image_path or "").strip()
    if not img or not os.path.isfile(img):
        return {"success": False, "message": f"图片不存在: {image_path}"}
    if not _image_usable(img, min_bytes=8000):
        return {"success": False, "message": "图片太小或是黑屏，请换一张 Steam+R1 截图"}

    crop_mode = str(crop or "none").strip().lower() or "none"
    crop_align = str(align or "center").strip().lower() or "center"
    crop_info: Dict[str, Any] = {}
    if crop_mode and crop_mode != "none":
        cropped = crop_image_for_icon(img, mode=crop_mode, align=crop_align, max_edge=0)
        if cropped.get("success") and cropped.get("path"):
            img = str(cropped["path"])
            crop_info = cropped
        else:
            return {
                "success": False,
                "message": cropped.get("message") or "裁剪失败",
            }

    sid = resolve_primary_userdata_id(userdata_id or "")
    if not sid:
        return {"success": False, "message": "找不到 Steam 用户目录"}

    if max_edge is None:
        max_edge = (load_settings() or {}).get("screenshot_max_edge", 768)
    cap_edge = _normalize_screenshot_max_edge(max_edge, 768)
    # 列表小图标：有上限，避免过大
    if cap_edge <= 0:
        icon_edge = 256
    else:
        icon_edge = max(128, min(512, cap_edge))

    info = prepare_steam_icon(
        img,
        appid_u,
        sid,
        name=name or "",
        icon_max_edge=icon_edge,
        capsule_max_edge=cap_edge,
    )
    icon_path = info.get("icon") or ""
    if icon_path:
        _update_shortcut_icon_field(sid, appid_u, icon_path, key=key)

    ok = bool(info.get("grid_icon") or info.get("grid_capsule") or icon_path)
    size_txt = "原图" if cap_edge <= 0 else f"最长边 {cap_edge}px"
    return {
        "success": ok,
        "appid": appid_u,
        "userdata_id": sid,
        "source": os.path.realpath(img),
        "icon": icon_path,
        "grid_icon": info.get("grid_icon") or "",
        "grid_capsule": info.get("grid_capsule") or "",
        "logo_removed": bool(info.get("logo_removed")),
        "max_edge": cap_edge,
        "icon_max_edge": icon_edge,
        "crop": crop_mode,
        "align": crop_align,
        "crop_info": crop_info,
        "message": (
            f"已用截图设为图标/封面（{size_txt}"
            + (f"，裁剪 {crop_mode}/{crop_align}" if crop_mode != "none" else "")
            + "）。请完全退出 Steam 再打开以刷新显示。"
            if ok
            else "写入图标失败，请检查图片格式。"
        ),
    }


def set_game_icon_from_screenshot(
    appid: Any,
    userdata_id: str = "",
    name: str = "",
    key: str = "",
    mode: str = "latest",
    delay_ms: int = 600,
    max_age_sec: int = 0,
    max_edge: Any = None,
    crop: str = "none",
    align: str = "center",
    image_path: str = "",
) -> Dict[str, Any]:
    """截屏或使用最新截图，设为该游戏图标。

    mode:
      - capture: 立即截取当前屏幕
      - latest: 使用该 appid / 最近的 Steam 截图
      - capture_or_latest: 先尝试截屏，失败则用最新截图
      - file: 使用 image_path 指定的截图
    crop / align: 写入前裁剪
    """
    mode = str(mode or "latest").lower().strip()
    src = ""
    capture_info: Dict[str, Any] = {}
    crop_mode = str(crop or "none").strip().lower() or "none"
    crop_align = str(align or "center").strip().lower() or "center"

    if max_edge is None:
        max_edge = (load_settings() or {}).get("screenshot_max_edge", 768)
    cap_edge = _normalize_screenshot_max_edge(max_edge, 768)

    if mode == "file":
        src = _normalize(image_path) or str(image_path or "").strip()
        if not src or not os.path.isfile(src):
            return {"success": False, "message": "请先选择一张截图", "max_edge": cap_edge}
        if not _image_usable(src, min_bytes=8000):
            return {"success": False, "message": "所选图片太小或无效", "max_edge": cap_edge}

    if mode in ("capture", "capture_or_latest"):
        dest = os.path.join(
            _screenshots_data_dir(),
            f"app_{normalize_appid(appid)}_{int(__import__('time').time())}.png",
        )
        capture_info = capture_display_screenshot(dest, delay_ms=delay_ms)
        raw = capture_info.get("path") or ""
        if capture_info.get("success") and _image_usable(raw):
            src = raw
            if src and cap_edge > 0:
                scaled = os.path.join(
                    _screenshots_data_dir(),
                    f"app_{normalize_appid(appid)}_{int(__import__('time').time())}_{cap_edge}.png",
                )
                if _ffmpeg_to_png(src, scaled, max_edge=cap_edge) and _image_usable(scaled):
                    src = scaled
                    capture_info["scaled_path"] = scaled
                    capture_info["max_edge"] = cap_edge
        else:
            # 黑屏/失败都不当成功，后面改走 Steam 截图
            if raw and not _image_usable(raw):
                logger.warning("reject blank capture %s (%s bytes)", raw, os.path.getsize(raw) if os.path.isfile(raw) else 0)
            capture_info["success"] = False
            src = ""

    if not src:
        # Steam+R1 经常存到别的 appid 目录（如 7 / 12143614 / 64 位 gameid）
        src = find_latest_screenshot(
            appid=appid,
            userdata_id=userdata_id,
            max_age_sec=max_age_sec,
            prefer_appid=True,
        ) or ""
        if src and not _image_usable(src):
            src = ""
        if not src:
            src = find_latest_screenshot(
                appid=0,
                userdata_id=userdata_id,
                max_age_sec=max_age_sec or 1800,
                prefer_appid=False,
            ) or ""
            if src and not _image_usable(src):
                src = ""

    if not src:
        msg = capture_info.get("message") if capture_info else ""
        return {
            "success": False,
            "message": msg
            or "未找到可用截图。请在游戏中按 Steam+R1（或 F12）截一张，再重试「用最新截图设为图标」。",
            "capture": capture_info,
            "max_edge": cap_edge,
        }

    result = set_game_icon_from_image(
        appid=appid,
        image_path=src,
        userdata_id=userdata_id,
        name=name,
        key=key,
        max_edge=cap_edge,
        crop=crop_mode,
        align=crop_align,
    )
    result["capture"] = capture_info
    result["mode"] = mode
    result["max_edge"] = cap_edge
    size_txt = "原图" if cap_edge <= 0 else f"{cap_edge}px"
    if result.get("success") and capture_info.get("success"):
        result["message"] = (
            f"已截屏并设为图标（{capture_info.get('tool') or 'capture'}，{size_txt}）。"
            "请完全退出 Steam 再打开以刷新。"
        )
    elif result.get("success"):
        result["message"] = (
            f"已用最新截图设为图标：{os.path.basename(src)}（{size_txt}）。"
            "请完全退出 Steam 再打开以刷新。"
        )
    return result


def _steam_console_log_paths() -> List[str]:
    """只要 console_log.txt。console-linux.txt 很大且几乎没有 Game process 行，还会污染状态。"""
    root = find_steam_root()
    paths = []
    if root:
        paths.append(os.path.join(root, "logs", "console_log.txt"))
    paths.append(os.path.expanduser("~/.steam/steam/logs/console_log.txt"))
    paths.append(os.path.expanduser("~/.local/share/Steam/logs/console_log.txt"))
    out = []
    seen = set()
    for p in paths:
        try:
            rp = os.path.realpath(p)
        except Exception:  # noqa: BLE001
            rp = p
        if rp not in seen and os.path.isfile(rp):
            seen.add(rp)
            out.append(rp)
    return out


_PROC_APPID_RE = re.compile(r"(?:AppId|AppID)\s*[=:]\s*(-?\d+)", re.I)
_LOG_PROCESS_RE = re.compile(
    r"Game process\s+(added|removed)\s*:\s*AppID\s+(-?\d+)",
    re.I,
)


def _iter_proc_cmdlines() -> List[str]:
    parts: List[str] = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fp:
                    raw = fp.read()
                if not raw:
                    continue
                parts.append(raw.replace(b"\x00", b" ").decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return parts


def _running_appids_from_proc() -> List[int]:
    """从仍活着的进程命令行读取 SteamLaunch AppId=（比日志可靠）。"""
    found: List[int] = []
    seen = set()
    for text in _iter_proc_cmdlines():
        if "SteamLaunch" not in text and "AppId=" not in text and "AppID" not in text:
            continue
        # 跳过 steamwebhelper / pressure-vessel 包装 Steam 自身
        low = text.lower()
        if "steamwebhelper" in low or "steamsysinfo" in low:
            continue
        for m in _PROC_APPID_RE.finditer(text):
            try:
                aid = normalize_appid(int(m.group(1)))
            except Exception:  # noqa: BLE001
                continue
            if not aid or aid in seen:
                continue
            # 过滤明显不是游戏的小 id（Steam 客户端本身）
            if aid < 10:
                continue
            seen.add(aid)
            found.append(aid)
    return found


def _parse_running_appids_from_steam_log() -> List[int]:
    """从 Steam 日志解析仍在运行的 AppID（added 且尚未 removed）。"""
    state: Dict[int, str] = {}
    for path in _steam_console_log_paths():
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fp:
                if size > 768 * 1024:
                    fp.seek(-768 * 1024, os.SEEK_END)
                data = fp.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        for line in data.splitlines():
            m = _LOG_PROCESS_RE.search(line)
            if not m:
                continue
            kind = m.group(1).lower()
            try:
                aid = normalize_appid(int(m.group(2)))
            except Exception:  # noqa: BLE001
                continue
            if not aid:
                continue
            state[aid] = "added" if kind == "added" else "removed"
    return [a for a, s in state.items() if s == "added"]


def _cmdline_blob() -> str:
    """汇总 /proc/*/cmdline 便于匹配 exe 路径。"""
    return "\n".join(_iter_proc_cmdlines())


def find_running_nonsteam_game() -> Dict[str, Any]:
    """检测当前正在运行的非 Steam 快捷方式游戏。

    优先级：
      1) Steam 日志中仍在 running 的非 Steam AppID
      2) /proc 命令行匹配 shortcuts 的 exe 路径
    """
    # 延迟导入避免循环：函数内调用 list 逻辑
    root = find_steam_root()
    games: List[Dict[str, Any]] = []
    if root:
        userdata = os.path.join(root, "userdata")
        if os.path.isdir(userdata):
            for sid in sorted(os.listdir(userdata)):
                sc_path = os.path.join(userdata, sid, "config", "shortcuts.vdf")
                if not os.path.isfile(sc_path):
                    continue
                try:
                    with open(sc_path, "rb") as fp:
                        parsed = _read_node(fp)
                except Exception:  # noqa: BLE001
                    continue
                shortcuts = parsed.get("shortcuts") or {}
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
                    try:
                        appid = int(appid_raw) if appid_raw is not None else compute_appid(exe, name)
                    except Exception:  # noqa: BLE001
                        appid = compute_appid(exe, name)
                    games.append(
                        {
                            "appid": normalize_appid(appid),
                            "name": name,
                            "exe": exe,
                            "start_dir": entry.get("StartDir") or "",
                            "userdata_id": sid,
                            "key": key,
                        }
                    )

    by_appid = {normalize_appid(g["appid"]): g for g in games if g.get("appid")}

    def _hit(aid: int, source: str) -> Optional[Dict[str, Any]]:
        u = normalize_appid(aid)
        if not u or u not in by_appid:
            return None
        if not is_nonsteam_shortcut_appid(u):
            return None
        g = by_appid[u]
        logger.info("running game detected source=%s appid=%s name=%s", source, u, g.get("name"))
        return {
            "running": True,
            "source": source,
            "appid": u,
            "game": g,
            "message": f"正在运行：{g.get('name') or u}",
        }

    # 1) 活进程里的 AppId=（游戏中开 QAM 时最准）
    for aid in reversed(_running_appids_from_proc()):
        hit = _hit(aid, "proc_appid")
        if hit:
            return hit

    # 2) Steam 日志 added 且尚未 removed
    for aid in reversed(_parse_running_appids_from_steam_log()):
        hit = _hit(aid, "steam_log")
        if hit:
            return hit

    # 进程匹配（避免 flatpak/python 等通用启动器误报）
    _GENERIC_EXES = {
        "flatpak",
        "python",
        "python3",
        "bash",
        "sh",
        "zsh",
        "wine",
        "wine64",
        "proton",
        "reaper",
        "steam-launch-wrapper",
        "steam",
        "gamesoverlayui",
        "pressure-vessel",
        "pv-adverb",
        "bwrap",
    }
    blob = _cmdline_blob()
    blob_l = blob.lower()
    if blob_l:
        hits = []
        for g in games:
            exe_n = (_normalize(g.get("exe") or "") or "").replace("\\", "/")
            if not exe_n:
                continue
            base = os.path.basename(exe_n).lower()
            # 通用启动器：要求命令行里同时出现游戏目录或 AppName 片段
            if base in _GENERIC_EXES or len(base) < 5:
                start_n = (_normalize(g.get("start_dir") or "") or "").replace("\\", "/").rstrip("/")
                name = str(g.get("name") or "").strip()
                score = 0
                if start_n and len(start_n) >= 12 and start_n.lower() in blob_l:
                    score += 3
                if name and len(name) >= 4 and name.lower() in blob_l:
                    score += 2
                # flatpak app id 常在 LaunchOptions / 路径中——用 start 或 name 不足则跳过
                if score < 3:
                    continue
                hits.append((score, g))
                continue
            # 正常 exe：优先完整路径，其次 basename（且 basename 足够长）
            full = exe_n.lower()
            if full and len(full) >= 12 and full in blob_l:
                hits.append((5, g))
            elif base and len(base) >= 6 and base in blob_l:
                # basename 命中给较低分，后面取最高分
                hits.append((2, g))
        if hits:
            hits.sort(key=lambda x: x[0], reverse=True)
            g = hits[0][1]
            return {
                "running": True,
                "source": "process",
                "appid": normalize_appid(g.get("appid")),
                "game": g,
                "message": f"正在运行：{g.get('name') or g.get('appid')}",
            }

    return {
        "running": False,
        "source": "",
        "appid": 0,
        "game": None,
        "message": "当前没有检测到正在运行的非 Steam 游戏（可在下方列表手动选择）",
    }


def fix_missing_title_logos(userdata_id: str = "") -> Dict[str, Any]:
    """为库中非 Steam 项删除误写的 _logo.png，恢复详情页名称栏文字。"""
    root = find_steam_root()
    if not root:
        return {"success": False, "message": "找不到 Steam", "removed": 0}

    settings = load_settings()
    sid = resolve_primary_userdata_id(userdata_id or settings.get("userdata_id") or "")
    if not sid:
        return {"success": False, "message": "找不到用户目录", "removed": 0}

    sc_path = os.path.join(root, "userdata", sid, "config", "shortcuts.vdf")
    if not os.path.isfile(sc_path):
        return {"success": True, "message": "无 shortcuts", "removed": 0}

    try:
        with open(sc_path, "rb") as fp:
            parsed = _read_node(fp)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": str(e), "removed": 0}

    removed = 0
    details = []
    for _k, entry in (parsed.get("shortcuts") or {}).items():
        if not isinstance(entry, dict):
            continue
        appid = normalize_appid(entry.get("appid"))
        if not appid:
            continue
        if _remove_grid_logo(appid, sid):
            removed += 1
            details.append(
                {
                    "appid": appid,
                    "name": entry.get("AppName") or "",
                }
            )

    return {
        "success": True,
        "removed": removed,
        "details": details[:80],
        "message": (
            f"已删除 {removed} 个误写的 logo，详情页名称栏将显示 AppName 文字。"
            "请完全退出 Steam 再打开以刷新。"
            if removed
            else "未发现需要删除的 logo。"
        ),
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
            if lower.endswith(_LINUX_LAUNCHER_EXTS):
                _ensure_executable(full)
            exe_n = os.path.realpath(full)
            start = os.path.dirname(exe_n) + os.sep
            name = _guess_game_name(exe_n, scan_path)
            score = _score_exe(exe_n, scan_path)
            is_hidden = exe_n in hidden_set
            is_trouble = _path_has_trouble(exe_n) or _has_trouble_suffix(name)
            game_folder = _resolve_game_folder(exe_n, scan_path)
            icon_path = find_icon_for_exe(exe_n, start) or ""
            # 前端直接用 data URL 画图（file:// 在 Decky/CEF 里通常显示不了）
            icon_data_url = load_icon_data_url(icon_path) if icon_path else ""
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
                    "trouble": is_trouble,
                    "game_folder": game_folder,
                    "icon": icon_path,
                    "has_icon": bool(icon_path),
                    "icon_data_url": icon_data_url,
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

    # 问题项靠后排，方便先处理正常候选
    visible.sort(
        key=lambda x: (
            bool(x.get("trouble")),
            x["already_added"],
            -x["score"],
            x["name"].lower(),
        )
    )
    hidden_only.sort(
        key=lambda x: (bool(x.get("trouble")), -x["score"], x["name"].lower())
    )
    trouble_count = sum(1 for x in pruned if x.get("trouble"))

    return {
        "games": visible[:300],
        "hidden_games": hidden_only[:300],
        "extract": extract_info,
        "scan_path": scan_path,
        "hidden_count_settings": len(hidden_set),
        "trouble_count": trouble_count,
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
        start = _normalize(raw.get("start_dir") or "") or (
            os.path.dirname(exe_path) if exe_path else ""
        )
        if not exe_path or not os.path.isfile(exe_path):
            skipped.append({"exe": raw.get("exe"), "reason": "文件不存在"})
            continue
        # 显示名：始终按文件夹名，不用 exe 文件名（即使前端传了 exe 名也覆盖）
        settings_scan = str((load_settings() or {}).get("scan_path") or "")
        folder_name = _guess_game_name(exe_path, settings_scan or start)
        raw_name = str(raw.get("name") or "").strip()
        exe_stem = os.path.splitext(os.path.basename(exe_path))[0]
        # 若传入名几乎等于 exe 主名，改用文件夹名
        if not raw_name or raw_name.lower() == exe_stem.lower() or raw_name.endswith(".exe"):
            name = folder_name or raw_name or exe_stem
        else:
            # 前端扫描已给出文件夹名时保留
            name = raw_name or folder_name or exe_stem
        # 再保险：若最终仍是 exe 主名且文件夹名不同，强制用文件夹
        if folder_name and name.lower() == exe_stem.lower() and folder_name.lower() != exe_stem.lower():
            name = folder_name
        if exe_path in existing_exes:
            skipped.append({"exe": exe_path, "name": name, "reason": "已在库中"})
            continue

        appid = compute_appid(exe_path, name)
        steamexe = _format_exe_for_steam(exe_path)
        steamdir = _format_startdir_for_steam(start)

        # 图标：优先用前端传入，否则自动在启动器旁查找
        icon_src = str(raw.get("icon") or "").strip()
        if icon_src:
            icon_src = _normalize(icon_src) or icon_src
        if not icon_src or not os.path.isfile(icon_src):
            icon_src = find_icon_for_exe(exe_path, start) or ""

        if icon_src:
            icon_info = prepare_steam_icon(icon_src, appid, sid, name=name)
        else:
            # 无图标也清掉误写 logo，保证详情页显示名称
            icon_info = {
                "icon": "",
                "grid_icon": "",
                "source": "",
                "logo_removed": _remove_grid_logo(appid, sid),
            }
        steam_icon = icon_info.get("icon") or ""

        entry = {
            "appid": appid_to_steam_int32(appid),
            "AppName": name,
            "Exe": steamexe,
            "StartDir": steamdir,
            "icon": steam_icon,
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
                "icon": steam_icon,
                "icon_source": icon_info.get("source") or "",
                "grid_icon": icon_info.get("grid_icon") or "",
                "has_icon": bool(steam_icon),
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

    async def mark_scan_items_trouble(self, exes: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """将勾选启动项的游戏文件夹重命名为 xxx-trouble（不删除）。"""
        if isinstance(exes, dict):
            kwargs = {**exes, **kwargs}
            exes = kwargs.get("exes")
        if exes is None:
            exes = kwargs.get("exes") or []
        if not isinstance(exes, list):
            return {"success": False, "message": "exes 必须是路径数组", "done": [], "skipped": []}
        mark = kwargs.get("mark", True)
        if isinstance(mark, str):
            mark = mark.lower() not in ("0", "false", "no", "off")
        scan_path = str(kwargs.get("scan_path") or "")
        name = str(kwargs.get("name") or "")
        dry_run = bool(kwargs.get("dry_run") or kwargs.get("preview"))
        return mark_games_trouble(
            exes, scan_root=scan_path, mark=bool(mark), name=name, dry_run=dry_run
        )

    async def unmark_scan_items_trouble(self, exes: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """去掉游戏文件夹的 -trouble 后缀。"""
        if isinstance(exes, dict):
            kwargs = {**exes, **kwargs}
            exes = kwargs.get("exes")
        if exes is None:
            exes = kwargs.get("exes") or []
        if not isinstance(exes, list):
            return {"success": False, "message": "exes 必须是路径数组", "done": [], "skipped": []}
        scan_path = str(kwargs.get("scan_path") or "")
        name = str(kwargs.get("name") or "")
        dry_run = bool(kwargs.get("dry_run") or kwargs.get("preview"))
        return mark_games_trouble(
            exes, scan_root=scan_path, mark=False, name=name, dry_run=dry_run
        )

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
        tc = result.get("trouble_count") or sum(1 for g in games if g.get("trouble"))
        if tc:
            parts.append(f"问题标记(-trouble) {tc} 个")
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
            "trouble_count": tc,
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
        result = add_games_to_steam(entries, userdata_id=str(userdata_id or kwargs.get("userdata_id") or ""))
        # 补充提示：有多少项写入了 Steam 图标
        if result.get("success") and result.get("added"):
            with_icon = sum(1 for a in result["added"] if a.get("has_icon") or a.get("icon"))
            result["message"] = (
                result.get("message")
                or f"已添加 {len(result['added'])} 个"
            ) + f" 其中 {with_icon} 个已写入库图标；请完全退出 Steam 再打开以刷新。"
        return result

    async def get_cjk_font_lang_options(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """返回「修复汉化字体」支持的语言列表。"""
        opts = [
            {"id": k, "label": v.get("label", k)}
            for k, v in CJK_LANG_PRESETS.items()
        ]
        return {
            "success": True,
            "options": opts,
            "default": "zh_CN",
            "font_sizes": CJK_FONT_SIZE_OPTIONS,
            "default_font_size": 24,
        }

    async def repair_cjk_fonts(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """修复汉化字体（批量或单个）。

        参数（均可放在 dict 位置参数或 kwargs 中）:
          - lang: zh_CN | ja_JP | zh_TW（默认 zh_CN）
          - appid / appids: 指定单个或多个；为空则处理全部非 Steam 游戏
          - userdata_id / key / name: 单修时可选，便于精确写启动项
          - only_with_prefix: 仅处理已有 compatdata 的项
          - font_size: 0=不改；24/28/32=RPG Maker 默认字号
        """
        if isinstance(_arg, dict):
            kwargs = {**_arg, **kwargs}
        lang = str(kwargs.get("lang") or "zh_CN")
        only_with_prefix = bool(kwargs.get("only_with_prefix", False))
        font_size = kwargs.get("font_size", 0)
        appid = kwargs.get("appid")
        appids = kwargs.get("appids")
        userdata_id = str(kwargs.get("userdata_id") or "")
        key = str(kwargs.get("key") if kwargs.get("key") is not None else "")
        name = str(kwargs.get("name") or "")
        start_dir = str(kwargs.get("start_dir") or "")
        exe = str(kwargs.get("exe") or "")

        # 单游戏
        if appid is not None and appid != "" and not appids:
            if not start_dir or not exe:
                for g in await self.get_non_steam_games():
                    if normalize_appid(g.get("appid")) == normalize_appid(appid):
                        start_dir = start_dir or str(g.get("start_dir") or "")
                        exe = exe or str(g.get("exe") or "")
                        name = name or str(g.get("name") or "")
                        userdata_id = userdata_id or str(g.get("userdata_id") or "")
                        if not key:
                            key = str(g.get("key") if g.get("key") is not None else "")
                        break
            return repair_cjk_fonts_for_game(
                appid=appid,
                userdata_id=userdata_id,
                key=key,
                name=name,
                lang=lang,
                start_dir=start_dir,
                exe=exe,
                font_size=font_size,
                collect_prefix_dirs=_collect_prefix_dirs,
                find_steam_root=find_steam_root,
                normalize_appid=normalize_appid,
                read_node=_read_node,
                write_vdf=write_vdf,
            )

        games = await self.get_non_steam_games()
        id_list = None
        if appids is not None:
            if isinstance(appids, list):
                id_list = appids
            else:
                id_list = [appids]
        elif appid is not None and appid != "":
            id_list = [appid]

        return repair_cjk_fonts_batch(
            appids=id_list,
            lang=lang,
            only_with_prefix=only_with_prefix,
            font_size=font_size,
            games=games,
            collect_prefix_dirs=_collect_prefix_dirs,
            find_steam_root=find_steam_root,
            normalize_appid=normalize_appid,
            read_node=_read_node,
            write_vdf=write_vdf,
        )

    async def repair_nonsteam_icons(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """为已在库中的非 Steam 游戏补写 grid 图标（从 exe/旁路图标提取）。"""
        games = await self.get_non_steam_games()
        fixed = []
        skipped = []
        for g in games:
            exe = g.get("exe") or ""
            start = g.get("start_dir") or ""
            appid = normalize_appid(g.get("appid"))
            sid = str(g.get("userdata_id") or "")
            if not appid or not sid:
                skipped.append({"name": g.get("name"), "reason": "no_appid"})
                continue
            gname = str(g.get("name") or "")
            icon_src = find_icon_for_exe(exe, start) or ""
            if not icon_src:
                # 即使没图标也删掉误写 logo，恢复详情页名称
                if _remove_grid_logo(appid, sid):
                    fixed.append(
                        {
                            "name": gname,
                            "appid": appid,
                            "icon": "",
                            "grid_icon": "",
                            "logo_removed": True,
                        }
                    )
                else:
                    skipped.append({"name": gname, "reason": "no_icon_source"})
                continue
            info = prepare_steam_icon(icon_src, appid, sid, name=gname)
            # 同步写回 shortcuts.vdf 的 icon 字段
            if info.get("icon"):
                try:
                    root = find_steam_root()
                    sc_path = os.path.join(root, "userdata", sid, "config", "shortcuts.vdf")
                    if os.path.isfile(sc_path):
                        with open(sc_path, "rb") as fp:
                            parsed = _read_node(fp)
                        shortcuts = parsed.get("shortcuts") or {}
                        key = str(g.get("key") if g.get("key") is not None else "")
                        if key in shortcuts and isinstance(shortcuts[key], dict):
                            shortcuts[key]["icon"] = info["icon"]
                            write_vdf(sc_path, parsed)
                except Exception as e:  # noqa: BLE001
                    logger.warning("update shortcut icon field failed: %s", e)
            fixed.append(
                {
                    "name": gname,
                    "appid": appid,
                    "icon": info.get("icon"),
                    "grid_icon": info.get("grid_icon"),
                    "logo_removed": bool(info.get("logo_removed")),
                }
            )
        return {
            "success": True,
            "fixed_count": len(fixed),
            "skipped_count": len(skipped),
            "fixed": fixed[:50],
            "skipped": skipped[:50],
            "message": (
                f"已为 {len(fixed)} 个非 Steam 项补写图标（并清除会遮挡名称的 logo），"
                f"跳过 {len(skipped)} 个。请完全退出 Steam 再打开。"
            ),
        }

    async def fix_game_page_titles(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """删除误写的 _logo.png，恢复 Steam 游戏详情页名称栏文字。"""
        userdata_id = ""
        if isinstance(_arg, dict):
            kwargs = {**_arg, **kwargs}
        userdata_id = str(kwargs.get("userdata_id") or "")
        return fix_missing_title_logos(userdata_id=userdata_id)

    async def set_icon_from_screenshot(self, appid: Any = 0, **kwargs: Any) -> Dict[str, Any]:
        """截屏或用最新截图，设为指定非 Steam 游戏的库图标。

        参数（均可放 kwargs）:
          appid, userdata_id, name, key,
          mode: capture | latest | capture_or_latest
          delay_ms, max_age_sec,
          max_edge / screenshot_max_edge: 输出最长边(0=原图)，并写入设置
        """
        if isinstance(appid, dict):
            kwargs = {**appid, **kwargs}
            appid = kwargs.get("appid", 0)
        try:
            aid = normalize_appid(kwargs.get("appid", appid) or 0)
        except Exception:  # noqa: BLE001
            aid = 0
        if not aid:
            return {"success": False, "message": "缺少 appid"}

        # 尽量补全 name / userdata_id / key
        games = await self.get_non_steam_games()
        match = None
        for g in games:
            if normalize_appid(g.get("appid")) == aid:
                match = g
                break
        sid = str(kwargs.get("userdata_id") or (match or {}).get("userdata_id") or "")
        name = str(kwargs.get("name") or (match or {}).get("name") or "")
        key = str(kwargs.get("key") if kwargs.get("key") is not None else (match or {}).get("key") or "")
        mode = str(kwargs.get("mode") or "capture_or_latest")
        try:
            delay_ms = int(kwargs.get("delay_ms", 600) or 600)
        except Exception:  # noqa: BLE001
            delay_ms = 600
        try:
            max_age_sec = int(kwargs.get("max_age_sec", 0) or 0)
        except Exception:  # noqa: BLE001
            max_age_sec = 0

        # 尺寸：优先本次参数，否则用已保存设置
        max_edge = kwargs.get("max_edge", kwargs.get("screenshot_max_edge", None))
        if max_edge is not None:
            max_edge = _normalize_screenshot_max_edge(max_edge, 768)
            try:
                save_settings({"screenshot_max_edge": max_edge})
            except Exception as e:  # noqa: BLE001
                logger.warning("save screenshot_max_edge failed: %s", e)

        result = set_game_icon_from_screenshot(
            appid=aid,
            userdata_id=sid,
            name=name,
            key=key,
            mode=mode,
            delay_ms=delay_ms,
            max_age_sec=max_age_sec,
            max_edge=max_edge,
            crop=str(kwargs.get("crop") or kwargs.get("crop_mode") or "none"),
            align=str(kwargs.get("align") or kwargs.get("crop_align") or "center"),
            image_path=str(kwargs.get("image_path") or kwargs.get("path") or ""),
        )
        logger.info(
            "set_icon_from_screenshot appid=%s mode=%s edge=%s ok=%s src=%s",
            aid,
            mode,
            result.get("max_edge"),
            result.get("success"),
            result.get("source") or (result.get("capture") or {}).get("path"),
        )
        return result

    async def capture_and_set_icon(self, appid: Any = 0, **kwargs: Any) -> Dict[str, Any]:
        """立即截屏并设为当前游戏图标（mode=capture）。"""
        if isinstance(appid, dict):
            kwargs = {**appid, **kwargs}
            appid = kwargs.get("appid", 0)
        kwargs["mode"] = "capture"
        return await self.set_icon_from_screenshot(appid=appid, **kwargs)

    async def set_icon_from_latest_screenshot(self, appid: Any = 0, **kwargs: Any) -> Dict[str, Any]:
        """用最新截图设为当前游戏图标（mode=latest）。"""
        if isinstance(appid, dict):
            kwargs = {**appid, **kwargs}
            appid = kwargs.get("appid", 0)
        kwargs["mode"] = "latest"
        if "max_age_sec" not in kwargs:
            kwargs["max_age_sec"] = 0
        return await self.set_icon_from_screenshot(appid=appid, **kwargs)

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
        return list_all_nonsteam_games()

    async def get_plugin_status(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """面板顶部状态：Steam 是否在跑、库数量、重复项。"""
        games = list_all_nonsteam_games()
        dups = find_duplicate_nonsteam_groups(games)
        return {
            "success": True,
            "steam_running": is_steam_running(),
            "game_count": len(games),
            "duplicate_groups": dups.get("count") or 0,
            "message": (
                "Steam 正在运行，改库后请完全退出再打开。"
                if is_steam_running()
                else "Steam 未在运行。"
            ),
        }

    async def find_duplicate_nonsteam_games(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return find_duplicate_nonsteam_groups()

    async def purge_duplicate_shortcuts(
        self,
        _arg: Any = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """每组 same_exe 重复项只保留一条快捷方式（不删文件）。"""
        if isinstance(_arg, dict):
            kwargs = {**_arg, **kwargs}
        keep_first = bool(kwargs.get("keep_first", True))
        groups = find_duplicate_nonsteam_groups().get("groups") or []
        removed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for grp in groups:
            if grp.get("reason") != "same_exe":
                continue
            items = list(grp.get("games") or [])
            if len(items) < 2:
                continue
            # 保留第一条（key 较小的优先）
            items.sort(key=lambda x: (str(x.get("key") or ""), normalize_appid(x.get("appid"))))
            keep = items[0] if keep_first else items[-1]
            for item in items:
                if item is keep:
                    continue
                if normalize_appid(item.get("appid")) == normalize_appid(keep.get("appid")):
                    if str(item.get("key")) == str(keep.get("key")):
                        continue
                try:
                    rm = remove_shortcuts_from_steam(
                        userdata_id=str(item.get("userdata_id") or ""),
                        key=str(item.get("key") if item.get("key") is not None else ""),
                        appid=0,
                        exe="",
                        name="",
                    )
                    if rm.get("removed"):
                        removed.append(
                            {
                                "name": item.get("name"),
                                "appid": normalize_appid(item.get("appid")),
                                "kept": keep.get("appid"),
                            }
                        )
                    else:
                        failed.append({"name": item.get("name"), "message": rm.get("message")})
                except Exception as e:  # noqa: BLE001
                    failed.append({"name": item.get("name"), "error": str(e)})
        return {
            "success": True,
            "removed_count": len(removed),
            "failed_count": len(failed),
            "removed": removed,
            "failed": failed,
            "message": (
                f"已移除 {len(removed)} 条重复快捷方式（文件未删）"
                + (f"，失败 {len(failed)} 条" if failed else "")
                + "。请完全退出 Steam 再打开。"
            ),
        }

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

    async def get_running_nonsteam_game(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """检测当前是否有非 Steam 游戏在运行（供左侧插件截图设图标用）。"""
        return find_running_nonsteam_game()

    async def list_recent_screenshots(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """列出可选截图，供面板点选裁剪。"""
        if isinstance(_arg, dict):
            kwargs = {**_arg, **kwargs}
        try:
            limit = int(kwargs.get("limit") or 16)
        except Exception:  # noqa: BLE001
            limit = 16
        try:
            max_age = int(kwargs.get("max_age_sec") or 0)
        except Exception:  # noqa: BLE001
            max_age = 0
        return list_recent_screenshots(
            appid=kwargs.get("appid") or 0,
            userdata_id=str(kwargs.get("userdata_id") or ""),
            limit=limit,
            max_age_sec=max_age,
        )

    async def list_nonsteam_for_icon(self, _arg: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """列出库中非 Steam 游戏 + 当前运行项（左侧插件选择目标）。"""
        games = await self.get_non_steam_games()
        running = find_running_nonsteam_game()
        # 名称排序，运行中的置顶
        run_id = normalize_appid((running.get("game") or {}).get("appid")) if running.get("running") else 0
        def _key(g: Dict[str, Any]):
            aid = normalize_appid(g.get("appid"))
            return (0 if aid and aid == run_id else 1, str(g.get("name") or "").lower())
        games_sorted = sorted(games, key=_key)
        settings = load_settings()
        return {
            "success": True,
            "games": games_sorted,
            "count": len(games_sorted),
            "running": running,
            "screenshot_max_edge": settings.get("screenshot_max_edge", 768),
        }

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

            shared = start_dir_shared_with_others(start, appid) if start else []
            start_base = os.path.basename((start or "").rstrip("/")).lower()
            container = start_base in _TROUBLE_CONTAINERS or start_base in ("downloads", "games")
            # StartDir：仅当它足够“像游戏目录”且不被其它快捷方式共用时才整目录删除
            if start and os.path.isdir(start) and _safe_to_delete(start) and not shared and not container:
                targets.append(start)
            elif shared:
                logger.warning(
                    "start dir shared with %s, will not rmtree: %s",
                    [s.get("name") for s in shared[:4]],
                    start,
                )
            # 可执行文件：始终尝试（若已在 start 目录内则不必重复）
            if exe and os.path.exists(exe) and _safe_to_delete(exe):
                if not start or not exe.startswith(start.rstrip("/") + "/"):
                    if exe not in targets:
                        targets.append(exe)
                elif start not in targets:
                    # start 未加入（不安全或共用）时至少删 exe
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
        start_n = _normalize(start_dir or "")
        shared = start_dir_shared_with_others(start_n, game["appid"]) if start_n else []
        running = find_running_nonsteam_game()
        game_running = bool(
            running.get("running")
            and normalize_appid(running.get("appid")) == game["appid"]
        )
        warnings: List[str] = []
        if is_steam_running():
            warnings.append("Steam 正在运行：写入 shortcuts 后请完全退出再打开，否则库列表可能被缓存盖回。")
        if game_running:
            warnings.append("该游戏似乎仍在运行，建议先退出再删，否则文件可能删不干净。")
        if shared:
            names = "、".join(str(s.get("name") or s.get("appid")) for s in shared[:4])
            warnings.append(f"启动目录与其它快捷方式共用（{names}），将只删本游戏 exe，不删整个目录。")
        return {
            "targets": targets,
            "existing": existing,
            "normalized_exe": _normalize(exe or ""),
            "normalized_start": start_n,
            "appid": game["appid"],
            "steam_running": is_steam_running(),
            "game_running": game_running,
            "shared_startdir": [s.get("name") for s in shared],
            "warnings": warnings,
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

    async def restart_steam_client(self) -> Dict[str, Any]:
        """强制结束 Steam 客户端进程，避免它退出时把内存里的旧 shortcuts/注册表
        数据重新写回磁盘，盖掉插件刚做的改动。需要用户在前端明确点击确认。"""
        return restart_steam_client()

    async def list_backup_files(self) -> List[Dict[str, Any]]:
        return list_backup_files()

    async def restore_backup_file(self, path: str = "", **kwargs: Any) -> Dict[str, Any]:
        if isinstance(path, dict):
            kwargs = {**path, **kwargs}
            path = kwargs.get("path", "")
        return restore_backup_file(str(path or ""))

    async def cleanup_backup_files(
        self,
        keep_latest: int = 3,
        older_than_days: int = 14,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if isinstance(keep_latest, dict):
            kwargs = {**keep_latest, **kwargs}
            keep_latest = kwargs.get("keep_latest", 3)
            older_than_days = kwargs.get("older_than_days", older_than_days)
        try:
            keep_latest = int(keep_latest)
        except Exception:  # noqa: BLE001
            keep_latest = 3
        try:
            older_than_days = int(older_than_days)
        except Exception:  # noqa: BLE001
            older_than_days = 14
        return cleanup_backup_files(keep_latest=keep_latest, older_than_days=older_than_days)
