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

from decky_plugin import Plugin, ripple

logger = logging.getLogger("NonSteamCleaner")

STEAM_ROOTS = [
    os.path.expanduser("~/.steam/steam"),
    os.path.expanduser("~/.local/share/Steam"),
    "/home/deck/.steam/steam",
    "/home/deck/.local/share/Steam",
]

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


def _write_node(fp, node: dict):
    for key, value in node.items():
        if isinstance(value, dict):
            fp.write(b"\x00")  # 子节点
            _write_cstring(fp, str(key))
            _write_node(fp, value)
            fp.write(b"\x08")  # 子节点结束
        elif isinstance(value, bool):
            fp.write(b"\x02")
            _write_cstring(fp, str(key))
            fp.write(struct.pack("<i", 1 if value else 0))
        elif isinstance(value, int):
            if value < 0 or value > 0x7FFFFFFF:
                fp.write(b"\x07")
                _write_cstring(fp, str(key))
                fp.write(struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))
            else:
                fp.write(b"\x02")
                _write_cstring(fp, str(key))
                fp.write(struct.pack("<i", value))
        elif isinstance(value, float):
            fp.write(b"\x03")
            _write_cstring(fp, str(key))
            fp.write(struct.pack("<f", value))
        else:  # 默认按字符串
            fp.write(b"\x01")
            _write_cstring(fp, str(key))
            _write_cstring(fp, str(value))


def write_vdf(path: str, root: dict):
    with open(path, "wb") as fp:
        _write_node(fp, root)
        fp.write(b"\x08")  # 根节点结束


# ---------------------------------------------------------------------------
# 路径辅助
# ---------------------------------------------------------------------------
def find_steam_root() -> Optional[str]:
    for r in STEAM_ROOTS:
        if os.path.isdir(r) and os.path.isdir(os.path.join(r, "steamapps")):
            return r
    return None


def compute_appid(exe: str, name: str) -> int:
    """Steam 对非 Steam 快捷方式 appid 的 CRC32 算法(回退用)。"""
    crc = zlib.crc32(b"SteamLaunch")
    crc = zlib.crc32(exe.lower().encode("utf-8"), crc)
    crc = zlib.crc32(name.encode("utf-8"), crc)
    return crc & 0xFFFFFFFF


def _normalize(p: str) -> Optional[str]:
    """将 Steam 的路径(可能带 Z: 盘符 / 反斜杠)转为真实绝对路径。"""
    if not p:
        return None
    p = p.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":  # 如 Z:/home/deck/...  ->  /home/deck/...
        p = p[2:]
    p = os.path.expanduser(p)
    return os.path.realpath(p)


def _safe_to_delete(p: str) -> bool:
    if not p:
        return False
    rp = os.path.realpath(p)
    if rp in _PROTECT_BASE:
        return False
    root = find_steam_root()
    if root and rp == os.path.realpath(root):
        return False
    # 至少要有三层路径，避免误删过浅的目录
    if len([x for x in rp.strip("/").split("/") if x]) < 3:
        return False
    return True


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------
class Plugin(Plugin):
    async def _main(self):
        pass

    async def _unload(self):
        pass

    # ---- 列出所有非 Steam 游戏 ----
    @ripple
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
                        "appid": appid,
                        "name": name,
                        "exe": exe,
                        "start_dir": entry.get("StartDir") or "",
                        "userdata_id": sid,
                        "key": key,
                    }
                )
        return results

    # ---- 计算将要删除的目标路径 ----
    def _compute_targets(
        self,
        game: Dict[str, Any],
        delete_body: bool,
        delete_saves: bool,
        delete_shader: bool,
    ) -> List[str]:
        root = find_steam_root()
        targets: List[str] = []
        appid = game["appid"]
        sid = game["userdata_id"]

        if delete_body:
            exe = _normalize(game.get("exe", ""))
            start = _normalize(game.get("start_dir", ""))
            if start and os.path.isdir(start) and _safe_to_delete(start):
                targets.append(start)
            if exe and os.path.exists(exe) and exe not in targets and _safe_to_delete(exe):
                targets.append(exe)

        if delete_saves:
            cd = os.path.join(root, "steamapps", "compatdata", str(appid))
            if os.path.isdir(cd) and _safe_to_delete(cd):
                targets.append(cd)

        if delete_shader:
            sc = os.path.join(root, "steamapps", "shadercache", str(appid))
            if os.path.isdir(sc) and _safe_to_delete(sc):
                targets.append(sc)

        # 清理孤儿网格图片(始终执行，因为快捷方式会被移除)
        grid_dir = os.path.join(root, "userdata", sid, "config", "grid")
        if os.path.isdir(grid_dir):
            grid_re = re.compile(rf"^{re.escape(str(appid))}(\.|p|_|$)")
            for f in glob.glob(os.path.join(grid_dir, f"{appid}*")):
                if grid_re.match(os.path.basename(f)):
                    targets.append(f)

        # 去重并保持顺序
        seen = set()
        out = []
        for t in targets:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    @ripple
    async def preview_delete(
        self,
        appid: int,
        userdata_id: str,
        exe: str,
        start_dir: str,
        delete_body: bool,
        delete_saves: bool,
        delete_shader: bool,
    ) -> Dict[str, Any]:
        game = {
            "appid": appid,
            "userdata_id": userdata_id,
            "exe": exe,
            "start_dir": start_dir,
        }
        targets = self._compute_targets(game, delete_body, delete_saves, delete_shader)
        existing = [t for t in targets if os.path.exists(t)]
        return {"targets": targets, "existing": existing}

    @ripple
    async def delete_non_steam_game(
        self,
        appid: int,
        userdata_id: str,
        key: str,
        exe: str,
        start_dir: str,
        delete_body: bool,
        delete_saves: bool,
        delete_shader: bool,
    ) -> Dict[str, Any]:
        root = find_steam_root()
        game = {
            "appid": appid,
            "userdata_id": userdata_id,
            "exe": exe,
            "start_dir": start_dir,
        }
        targets = self._compute_targets(game, delete_body, delete_saves, delete_shader)

        deleted: List[str] = []
        for t in targets:
            try:
                if os.path.islink(t):
                    os.unlink(t)
                elif os.path.isdir(t):
                    shutil.rmtree(t)
                elif os.path.exists(t):
                    os.remove(t)
                deleted.append(t)
            except Exception as e:  # noqa: BLE001
                logger.error("删除失败 %s: %s", t, e)

        # 从 shortcuts.vdf 移除快捷方式
        removed_shortcut = False
        sc_path = os.path.join(root, "userdata", userdata_id, "config", "shortcuts.vdf")
        if os.path.isfile(sc_path):
            try:
                with open(sc_path, "rb") as fp:
                    parsed = _read_node(fp)
                shortcuts = parsed.get("shortcuts", {})
                if isinstance(shortcuts, dict) and str(key) in shortcuts:
                    del shortcuts[str(key)]
                    write_vdf(sc_path, parsed)
                    removed_shortcut = True
            except Exception as e:  # noqa: BLE001
                logger.error("更新 shortcuts 失败: %s", e)

        return {"deleted": deleted, "removed_shortcut": removed_shortcut}
