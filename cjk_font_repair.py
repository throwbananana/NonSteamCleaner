"""
修复汉化字体：为非 Steam 游戏的 Proton 前缀设置中/日/繁区域，
补黑体字体映射，并写入 Steam 启动项 LANG/LC_ALL。

解决老汉化/日文 Windows 游戏在 Steam Deck 上文字显示为 ?? 的问题。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import struct
from typing import Any, Dict, List, Optional

logger = logging.getLogger("NonSteamCleaner")

CJK_LANG_PRESETS: Dict[str, Dict[str, str]] = {
    "zh_CN": {
        "label": "简体中文",
        "unix_lang": "zh_CN.UTF-8",
        "locale_name": "zh-CN",
        "lcid": "00000804",
        "lang_id": "0804",
        "acp": "936",
        "oemcp": "936",
        "maccp": "10008",
        "s_language": "CHS",
        "s_country": "China",
        "i_country": "86",
        "geo_name": "CN",
        "geo_nation": "45",
        "currency": "¥",
        "need_heiti": "1",
    },
    "ja_JP": {
        "label": "日文",
        "unix_lang": "ja_JP.UTF-8",
        "locale_name": "ja-JP",
        "lcid": "00000411",
        "lang_id": "0411",
        "acp": "932",
        "oemcp": "932",
        "maccp": "10001",
        "s_language": "JPN",
        "s_country": "Japan",
        "i_country": "81",
        "geo_name": "JP",
        "geo_nation": "122",
        "currency": "¥",
        "need_heiti": "0",
    },
    "zh_TW": {
        "label": "繁体中文",
        "unix_lang": "zh_TW.UTF-8",
        "locale_name": "zh-TW",
        "lcid": "00000404",
        "lang_id": "0404",
        "acp": "950",
        "oemcp": "950",
        "maccp": "10002",
        "s_language": "CHT",
        "s_country": "Taiwan",
        "i_country": "886",
        "geo_name": "TW",
        "geo_nation": "237",
        "currency": "NT$",
        "need_heiti": "1",
    },
}


def resolve_cjk_preset(lang: str) -> Dict[str, str]:
    key = (lang or "zh_CN").strip().replace("-", "_")
    aliases = {
        "zh": "zh_CN",
        "zh_cn": "zh_CN",
        "chs": "zh_CN",
        "chinese": "zh_CN",
        "chinese_simplified": "zh_CN",
        "sc": "zh_CN",
        "ja": "ja_JP",
        "ja_jp": "ja_JP",
        "jp": "ja_JP",
        "japanese": "ja_JP",
        "zh_tw": "zh_TW",
        "zh_hk": "zh_TW",
        "cht": "zh_TW",
        "chinese_traditional": "zh_TW",
        "tc": "zh_TW",
    }
    key = aliases.get(key.lower(), key if key in CJK_LANG_PRESETS else "zh_CN")
    if key not in CJK_LANG_PRESETS:
        key = "zh_CN"
    return {"key": key, **CJK_LANG_PRESETS[key]}


# 常见 Windows CJK 系统字体族名。老 galgame 常用的吉里吉里/NScripter/Artemis/Siglus 等引擎
# 不是 RPG Maker，不会被 install_game_local_cjk_fonts 的 RGSS/HTML Maker 检测命中，
# 但它们和普通 Windows 程序一样，往往直接按这些标准字体名向系统要字体。之前只有
# RGSS 检测到的具体字体名才会被补装到 Wine 前缀里；这里给所有游戏都补一套与引擎无关的
# 常见别名兜底，即使游戏用的不是 RPG Maker 也能覆盖到。
_COMMON_GAME_FONT_ALIASES: Dict[str, List[str]] = {
    "zh_CN": [
        "SimSun", "宋体", "NSimSun", "新宋体", "FangSong", "仿宋",
        "KaiTi", "楷体", "Microsoft YaHei", "微软雅黑",
    ],
    "zh_TW": [
        "MingLiU", "細明體", "PMingLiU", "新細明體",
        "Microsoft JhengHei", "微軟正黑體", "SimHei", "黑体",
    ],
    "ja_JP": [
        "MS UI Gothic", "MS Gothic", "ＭＳ ゴシック",
        "MS PGothic", "ＭＳ Ｐゴシック",
        "MS Mincho", "ＭＳ 明朝", "MS PMincho", "ＭＳ Ｐ明朝",
        "Meiryo", "メイリオ",
    ],
}


def default_common_font_aliases(preset: Dict[str, str]) -> List[Dict[str, str]]:
    """该语言下与引擎无关的常见系统字体族名兜底列表，供写入 Wine 前缀。"""
    names = _COMMON_GAME_FONT_ALIASES.get(preset.get("key") or "", [])
    return [{"family": fam, "file": _safe_font_filename(fam)} for fam in names]


def build_cjk_launch_options(existing: str, unix_lang: str) -> str:
    """合并/写入 LANG/LC_ALL 启动项，保留用户其它参数。"""
    existing = (existing or "").strip()
    parts = existing.split()
    kept: List[str] = []
    for p in parts:
        up = p.upper()
        if up.startswith("LANG=") or up.startswith("LC_ALL=") or up.startswith("HOST_LC_ALL="):
            continue
        if p == "%command%":
            continue
        kept.append(p)
    prefix = f"LANG={unix_lang} LC_ALL={unix_lang}"
    rest = " ".join(kept).strip()
    if rest:
        return f"{prefix} {rest} %command%"
    return f"{prefix} %command%"


def _set_reg_value_line(section: str, key: str, value: str) -> str:
    pat = re.compile(r'^"' + re.escape(key) + r'"="[^"]*"$', re.M)
    line = f'"{key}"="{value}"'
    if pat.search(section):
        return pat.sub(line, section)
    return section.rstrip("\n") + "\n" + line + "\n"


def patch_reg_text_locale(content: str, preset: Dict[str, str], *, is_system: bool) -> tuple:
    """返回 (new_content, changes:list)。"""
    changes: List[str] = []
    s = content
    acp = preset["acp"]
    oemcp = preset["oemcp"]
    maccp = preset["maccp"]
    lang_id = preset["lang_id"]
    lcid = preset["lcid"]
    locale_name = preset["locale_name"]
    s_language = preset["s_language"]
    s_country = preset["s_country"]
    i_country = preset["i_country"]
    geo_name = preset["geo_name"]
    geo_nation = preset["geo_nation"]
    currency = preset["currency"]
    unix_pair = f"{acp},{oemcp}"

    if is_system:
        for pat, rep, label in [
            (r'"ACP"="\d+"', f'"ACP"="{acp}"', "ACP"),
            (r'"OEMCP"="\d+"', f'"OEMCP"="{oemcp}"', "OEMCP"),
            (r'"MACCP"="\d+"', f'"MACCP"="{maccp}"', "MACCP"),
        ]:
            ns, n = re.subn(pat, rep, s, count=1)
            if n:
                s = ns
                changes.append(label)

        # Nls\\Language Default / InstallLanguage
        m = re.search(
            r"(\[System\\\\ControlSet001\\\\Control\\\\Nls\\\\Language\][^\[]*?)"
            r'("Default"=")\d+("\n"InstallLanguage"=")\d+(")',
            s,
        )
        if m:
            s = (
                s[: m.start()]
                + m.group(1)
                + m.group(2)
                + lang_id
                + m.group(3)
                + lang_id
                + m.group(4)
                + s[m.end() :]
            )
            changes.append("NlsLanguage")
        else:
            for m in re.finditer(r'"Default"="\d+"\n"InstallLanguage"="\d+"', s):
                ctx = s[max(0, m.start() - 100) : m.start()]
                if "Nls" in ctx and "Language" in ctx:
                    s = (
                        s[: m.start()]
                        + f'"Default"="{lang_id}"\n"InstallLanguage"="{lang_id}"'
                        + s[m.end() :]
                    )
                    changes.append("NlsLanguage")
                    break

        m2 = re.search(
            r"(\[System\\\\ControlSet001\\\\Control\\\\Nls\\\\Locale\][^\n]*\n#time=[^\n]*\n)@=\"\d+\"",
            s,
        )
        if m2:
            s = s[: m2.start()] + m2.group(1) + f'@="{lcid}"' + s[m2.end() :]
            changes.append("NlsLocale")
        elif re.search(r'@="00000\d+"', s):
            s2, n = re.subn(r'@="00000\d+"', f'@="{lcid}"', s, count=1)
            if n:
                s = s2
                changes.append("NlsLocale")

        # FontSubstitutes 各段
        start = 0
        fs_touched = False
        while True:
            i = s.find("FontSubstitutes]", start)
            if i < 0:
                break
            sec_start = s.rfind("[", 0, i)
            sec_end = s.find("\n[", i)
            if sec_end < 0:
                sec_end = len(s)
            section = s[sec_start:sec_end]
            if preset.get("need_heiti") == "1":
                section = _set_reg_value_line(section, "SimHei", "Microsoft YaHei")
                section = _set_reg_value_line(section, "黑体", "Microsoft YaHei")
                section = _set_reg_value_line(section, "MINGLAN", "MINGLAN")
                section = _set_reg_value_line(section, "MingLan", "MINGLAN")
                section = _set_reg_value_line(section, "MS Shell Dlg", "Microsoft YaHei")
                section = _set_reg_value_line(section, "MS Shell Dlg 2", "Microsoft YaHei")
            else:
                section = _set_reg_value_line(section, "MS Shell Dlg", "MS UI Gothic")
                section = _set_reg_value_line(section, "MS Shell Dlg 2", "MS UI Gothic")
            s = s[:sec_start] + section + s[sec_end:]
            start = sec_start + len(section)
            fs_touched = True
        if fs_touched:
            changes.append("FontSubstitutes")

        if preset.get("need_heiti") == "1" and '"MINGLAN (TrueType)"="MINGLAN.ttf"' not in s:
            if '"Microsoft YaHei (TrueType)"="msyh.ttf"' in s:
                s = s.replace(
                    '"Microsoft YaHei (TrueType)"="msyh.ttf"',
                    '"Microsoft YaHei (TrueType)"="msyh.ttf"\n'
                    '"MINGLAN (TrueType)"="MINGLAN.ttf"\n'
                    '"MingLan (TrueType)"="MINGLAN.ttf"',
                    1,
                )
                changes.append("RegisterMINGLAN")
            elif '"MINGLAN (TrueType)"="MINGLAN.ttf"' not in s and "CurrentVersion\\\\Fonts]" in s:
                s = s.replace(
                    "CurrentVersion\\\\Fonts]",
                    'CurrentVersion\\\\Fonts]\n"MINGLAN (TrueType)"="MINGLAN.ttf"',
                    1,
                )
                changes.append("RegisterMINGLAN")
        if preset.get("need_heiti") == "1" and '"SimHei (TrueType)"="simhei.ttf"' not in s:
            if '"Microsoft YaHei (TrueType)"="msyh.ttf"' in s:
                s = s.replace(
                    '"Microsoft YaHei (TrueType)"="msyh.ttf"',
                    '"Microsoft YaHei (TrueType)"="msyh.ttf"\n'
                    '"SimHei (TrueType)"="simhei.ttf"\n'
                    '"黑体 (TrueType)"="simhei.ttf"\n'
                    '"MINGLAN (TrueType)"="MINGLAN.ttf"\n'
                    '"MingLan (TrueType)"="MINGLAN.ttf"',
                    1,
                )
                changes.append("RegisterSimHei")
    else:
        m = re.search(r"(\[Control Panel\\\\International\][^\[]*)", s)
        if m:
            intl = m.group(1)
            for pat, rep in [
                (r'"Locale"="[^"]*"', f'"Locale"="{lcid}"'),
                (r'"LocaleName"="[^"]*"', f'"LocaleName"="{locale_name}"'),
                (r'"sLanguage"="[^"]*"', f'"sLanguage"="{s_language}"'),
                (r'"sCountry"="[^"]*"', f'"sCountry"="{s_country}"'),
                (r'"iCountry"="[^"]*"', f'"iCountry"="{i_country}"'),
                (r'"sCurrency"="[^"]*"', f'"sCurrency"="{currency}"'),
            ]:
                intl2, n = re.subn(pat, rep, intl, count=1)
                if n:
                    intl = intl2
            s = s[: m.start(1)] + intl + s[m.end(1) :]
            changes.append("International")

        s2, n = re.subn(r'"Name"="[A-Z]{2}"', f'"Name"="{geo_name}"', s, count=1)
        if n:
            s = s2
        s2, n = re.subn(r'"Nation"="\d+"', f'"Nation"="{geo_nation}"', s, count=1)
        if n:
            s = s2
            changes.append("Geo")

        s2, n = re.subn(r'"Codepages"="[^"]*"', f'"Codepages"="{unix_pair}"', s, count=1)
        if n:
            s = s2
            changes.append("WineCodepages")

        if preset.get("need_heiti") == "1":
            wm = re.search(
                r"(\[Software\\\\Wine\\\\Fonts\] \d+\n#time=[^\n]+\n)((?:\"[^\"]*\"=[^\n]+\n)*)",
                s,
            )
            if wm and "SimHei (TrueType)" not in wm.group(0):
                head, body = wm.group(1), wm.group(2)
                body = (
                    body
                    + '"SimHei (TrueType)"="simhei.ttf"\n'
                    + '"黑体 (TrueType)"="simhei.ttf"\n'
                    + '"SimHei"="simhei.ttf"\n'
                )
                s = s[: wm.start()] + head + body + s[wm.end() :]
                changes.append("WineFontsSimHei")

    return s, changes


def register_reg_font_families(content: str, families: List[Any], *, is_system: bool) -> tuple:
    """把游戏检测到的字体族写进 Wine Fonts / FontSubstitutes。"""
    if not families:
        return content, []
    items: List[tuple] = []
    seen = set()
    for item in families:
        if isinstance(item, dict):
            fam = str(item.get("family") or "")
            fn = str(item.get("file") or "")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            fam, fn = str(item[0]), str(item[1])
        else:
            continue
        if not fam:
            continue
        if not fn:
            fn = _safe_font_filename(fam)
        key = (fam, fn)
        if key in seen:
            continue
        seen.add(key)
        items.append(key)
    if not items:
        return content, []
    s = content
    changes: List[str] = []
    if is_system:
        lines = [f'"{fam} (TrueType)"="{fn}"' for fam, fn in items]
        missing = [ln for ln in lines if ln not in s]
        if missing and "CurrentVersion\\\\Fonts]" in s:
            s = s.replace(
                "CurrentVersion\\\\Fonts]",
                "CurrentVersion\\\\Fonts]\n" + "\n".join(missing),
                1,
            )
            changes.append("RegisterFamilies")
        start = 0
        fs_touched = False
        while True:
            i = s.find("FontSubstitutes]", start)
            if i < 0:
                break
            sec_start = s.rfind("[", 0, i)
            sec_end = s.find("\n[", i)
            if sec_end < 0:
                sec_end = len(s)
            section = s[sec_start:sec_end]
            for fam, _fn in items:
                section = _set_reg_value_line(section, fam, fam)
            s = s[:sec_start] + section + s[sec_end:]
            start = sec_start + len(section)
            fs_touched = True
        if fs_touched:
            changes.append("FamilySubstitutes")
    else:
        block = "".join(f'"{fam} (TrueType)"="{fn}"\n"{fam}"="{fn}"\n' for fam, fn in items)
        wm = re.search(
            r"(\[Software\\\\Wine\\\\Fonts\] \d+\n#time=[^\n]+\n)((?:\"[^\"]*\"=[^\n]+\n)*)",
            s,
        )
        if wm:
            already = wm.group(0)
            extra = "".join(
                ln
                for fam, fn in items
                for ln in (f'"{fam} (TrueType)"="{fn}"\n',)
                if f'"{fam} (TrueType)"=' not in already
            )
            if extra:
                s = s[: wm.start()] + wm.group(1) + wm.group(2) + extra + s[wm.end() :]
                changes.append("WineFontsFamilies")
        elif block and "Software\\\\Wine\\\\Fonts]" in s:
            s = s.replace("Software\\\\Wine\\\\Fonts]", "Software\\\\Wine\\\\Fonts]\n" + block, 1)
            changes.append("WineFontsFamilies")
    return s, changes


def _pick_cjk_ttf_source() -> str:
    """RGSS/RPG Maker 往往只认 TTF，不认 TTC。优先 Proton 自带的 msyh.ttf。"""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(
            home,
            ".local/share/Steam/steamapps/common/Proton - Experimental/files/share/fonts/msyh.ttf",
        ),
        os.path.join(
            home,
            ".steam/steam/steamapps/common/Proton - Experimental/files/share/fonts/msyh.ttf",
        ),
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    # 再扫 Proton* / Proton Experimental
    extra_roots = [
        os.path.join(home, ".local/share/Steam/steamapps/common"),
        os.path.join(home, ".steam/steam/steamapps/common"),
    ]
    for root in extra_roots:
        if not os.path.isdir(root):
            continue
        try:
            for name in os.listdir(root):
                if "roton" not in name:
                    continue
                p = os.path.join(root, name, "files/share/fonts/msyh.ttf")
                if os.path.isfile(p):
                    candidates.insert(0, p)
        except Exception:  # noqa: BLE001
            pass
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    # 写死路径都没找到（比如用的是 Proton-GE 而不是官方 Proton-Experimental，或者
    # 系统 Noto CJK 装在别的位置）：改用 fontconfig 现查系统里任意已装的 CJK 字体，
    # 不再要求命中固定路径。
    return _fc_match_cjk_font()


def _fc_match_cjk_font() -> str:
    """用 fc-match 查系统已安装的 CJK 字体文件路径，作为写死路径找不到时的通用兜底。"""
    import shutil as _shutil
    import subprocess

    if not _shutil.which("fc-match"):
        return ""
    for query in (
        "Noto Sans CJK SC",
        "Noto Sans CJK JP",
        "Noto Sans CJK",
        "Noto Serif CJK SC",
        "WenQuanYi Micro Hei",
        "WenQuanYi Zen Hei",
        "sans-serif:lang=zh-cn",
        "sans-serif:lang=ja",
    ):
        try:
            r = subprocess.run(
                ["fc-match", "-f", "%{file}", query],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
        except Exception:  # noqa: BLE001
            continue
        path = (r.stdout or b"").decode("utf-8", "replace").strip()
        if path and os.path.isfile(path) and path.lower().endswith((".ttf", ".ttc", ".otf")):
            return path
    return ""


def _ttf_checksum(data: bytes) -> int:
    if len(data) % 4:
        data = data + b"\x00" * (4 - len(data) % 4)
    s = 0
    for i in range(0, len(data), 4):
        s = (s + struct.unpack_from(">I", data, i)[0]) & 0xFFFFFFFF
    return s


def _sfnt_search_params(num_tables: int) -> tuple:
    entry_selector = 0
    n = num_tables
    while n > 1:
        n >>= 1
        entry_selector += 1
    search_range = (1 << entry_selector) * 16
    range_shift = num_tables * 16 - search_range
    return search_range, entry_selector, range_shift


def _first_font_from_ttc(raw: bytes) -> bytes:
    """把 TTC (TrueType Collection) 的第一个字体还原成独立的 sfnt 字节流。

    SteamOS/Arch 上的 Noto Sans CJK 就是以 .ttc 形式分发的（NotoSansCJK-Regular.ttc）。
    ttcf 签名不在 clone_ttf_with_family 认识的 (\\x00\\x01\\x00\\x00/true/OTTO) 里，
    不处理的话会被当成"非标准 TTF"整份原样拷贝，字体内部 name 表还是原始的
    "Noto Sans CJK ..."，改名完全不生效——游戏按目标字体名(如 SimHei)查询时还是找不到。
    """
    import struct as _st

    if len(raw) < 16 or raw[:4] != b"ttcf":
        return b""
    try:
        num_fonts = _st.unpack_from(">I", raw, 8)[0]
        if num_fonts < 1 or len(raw) < 12 + 4 * num_fonts:
            return b""
        offset = _st.unpack_from(">I", raw, 12)[0]
        if offset + 12 > len(raw):
            return b""
        sfnt_ver = raw[offset : offset + 4]
        num_tables = _st.unpack_from(">H", raw, offset + 4)[0]
        dir_end = offset + 12 + num_tables * 16
        if num_tables < 1 or dir_end > len(raw):
            return b""
        tables = []
        for i in range(num_tables):
            o = offset + 12 + i * 16
            tag = raw[o : o + 4]
            _check, toff, tlen = _st.unpack_from(">III", raw, o + 4)
            if toff + tlen > len(raw):
                return b""
            tables.append((tag, raw[toff : toff + tlen]))

        search_range, entry_selector, range_shift = _sfnt_search_params(num_tables)
        out = bytearray()
        out += sfnt_ver
        out += _st.pack(">HHHH", num_tables, search_range, entry_selector, range_shift)
        dir_pos = len(out)
        out += b"\x00" * (16 * num_tables)
        for tag, data in tables:
            if len(out) % 4:
                out += b"\x00" * (4 - len(out) % 4)
            toff = len(out)
            out += data
            check = _ttf_checksum(data)
            _st.pack_into(">4sIII", out, dir_pos, tag, check, toff, len(data))
            dir_pos += 16
        return bytes(out)
    except Exception:  # noqa: BLE001
        return b""


def clone_ttf_with_family(src: str, dest: str, family: str) -> str:
    """复制 TTF 并把字体族名改成 family。

    RGSS Font.exist? / AddFontResource 看的是 name 表，不是文件名。
    符号链接到雅黑时族名仍是 Microsoft YaHei，会报「未找到默认字体」。
    """
    import struct as _st

    if not src or not os.path.isfile(src):
        return "no_src"
    raw = open(src, "rb").read()
    if len(raw) >= 4 and raw[:4] == b"ttcf":
        extracted = _first_font_from_ttc(raw)
        if extracted:
            raw = extracted
    if len(raw) < 64 or raw[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        # 非标准 TTF，退回拷贝
        try:
            if os.path.islink(dest) or os.path.isfile(dest):
                os.remove(dest)
            shutil.copy2(src, dest)
            return "copy"
        except Exception as e:  # noqa: BLE001
            return f"error:{e}"

    num = _st.unpack_from(">H", raw, 4)[0]
    tables = []
    name_idx = -1
    for i in range(num):
        o = 12 + i * 16
        tag = raw[o : o + 4]
        check, toff, tlen = _st.unpack_from(">III", raw, o + 4)
        tables.append([tag, check, toff, tlen])
        if tag == b"name":
            name_idx = i
    if name_idx < 0:
        shutil.copy2(src, dest)
        return "copy-noname"

    fam = family
    ps = "".join(ch for ch in family if ch.isalnum()) or "Font"
    recs_u = [
        (0, "NscCJK"),
        (1, fam),
        (2, "Regular"),
        (3, f"{fam} Regular"),
        (4, fam),
        (5, "Version 1.0"),
        (6, ps),
        (16, fam),
    ]
    records = []
    # 中文族名不能写成 Mac Roman，否则 RGSS 读到 "????" 会报未找到默认字体。
    mac_ok = True
    try:
        fam.encode("latin-1")
    except UnicodeEncodeError:
        mac_ok = False
    for nid, text in recs_u:
        if mac_ok:
            records.append((1, 0, 0, nid, text.encode("latin-1", "replace")[:63]))
        records.append((3, 1, 0x0409, nid, text.encode("utf-16-be")))
        records.append((3, 1, 0x0804, nid, text.encode("utf-16-be")))
        records.append((3, 1, 0x0411, nid, text.encode("utf-16-be")))
    rec_blob = b""
    str_blob = b""
    for plat, enc, lang, nid, s in records:
        rec_blob += _st.pack(">HHHHHH", plat, enc, lang, nid, len(s), len(str_blob))
        str_blob += s
    new_name = _st.pack(">HHH", 0, len(records), 6 + 12 * len(records)) + rec_blob + str_blob
    if len(new_name) % 4:
        new_name += b"\x00" * (4 - len(new_name) % 4)
    new_check = _ttf_checksum(new_name)

    # 重排：去掉旧 name，把新 name 接到文件末尾
    old_off, old_len = tables[name_idx][2], tables[name_idx][3]
    # 简单做法：覆盖目录项指向追加数据
    out = bytearray(raw)
    new_off = len(out)
    if new_off % 4:
        pad = 4 - new_off % 4
        out += b"\x00" * pad
        new_off = len(out)
    out += new_name
    dir_o = 12 + name_idx * 16
    out[dir_o + 4 : dir_o + 16] = _st.pack(">III", new_check, new_off, len(new_name))

    # 重算 head.checkSumAdjustment
    for i, t in enumerate(tables):
        if t[0] == b"head":
            hoff = t[2]
            # 先把 adjustment 置 0
            out[hoff + 8 : hoff + 12] = b"\x00\x00\x00\x00"
            adj = (0xB1B0AFBA - _ttf_checksum(bytes(out))) & 0xFFFFFFFF
            out[hoff + 8 : hoff + 12] = _st.pack(">I", adj)
            break

    try:
        if os.path.lexists(dest):
            os.remove(dest)
        with open(dest, "wb") as fp:
            fp.write(out)
        return f"named:{family}"
    except Exception as e:  # noqa: BLE001
        return f"error:{e}"


def read_ttf_family_names(path: str) -> List[str]:
    """读取 TTF name 表里的族名（ID 1/4/16）。"""
    names: List[str] = []
    try:
        raw = open(path, "rb").read()
    except Exception:  # noqa: BLE001
        return names
    if len(raw) >= 4 and raw[:4] == b"ttcf":
        extracted = _first_font_from_ttc(raw)
        if extracted:
            raw = extracted
    if len(raw) < 64 or raw[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        return names
    num = struct.unpack_from(">H", raw, 4)[0]
    for i in range(num):
        o = 12 + i * 16
        if raw[o : o + 4] != b"name":
            continue
        _c, toff, tlen = struct.unpack_from(">III", raw, o + 4)
        name = raw[toff : toff + tlen]
        if len(name) < 6:
            return names
        _fmt, count, stroff = struct.unpack_from(">HHH", name, 0)
        for r in range(count):
            rec_o = 6 + r * 12
            if rec_o + 12 > len(name):
                break
            plat, enc, _lang, nid, nlen, noff = struct.unpack_from(">HHHHHH", name, rec_o)
            if nid not in (1, 4, 16):
                continue
            s = name[stroff + noff : stroff + noff + nlen]
            try:
                if plat == 3:
                    txt = s.decode("utf-16-be", "replace")
                elif plat == 1:
                    txt = s.decode("mac_roman", "replace")
                else:
                    txt = s.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            txt = txt.strip()
            if txt and txt not in names:
                names.append(txt)
        break
    return names


def ttf_has_family(path: str, family: str) -> bool:
    if not path or not os.path.isfile(path) or os.path.islink(path):
        return False
    want = (family or "").strip()
    if not want:
        return False
    names = read_ttf_family_names(path)
    return any(n == want for n in names)


def ttf_family_visible_to_rgss(path: str, family: str) -> bool:
    """RGSS Font.exist? 会按系统 ACP 读 0x409 / 0x804 的族名。两边都要有。"""
    if not path or not os.path.isfile(path) or os.path.islink(path):
        return False
    want = (family or "").strip()
    if not want:
        return False
    try:
        raw = open(path, "rb").read()
    except Exception:  # noqa: BLE001
        return False
    if len(raw) < 64 or raw[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        return False
    num = struct.unpack_from(">H", raw, 4)[0]
    seen = set()
    for i in range(num):
        o = 12 + i * 16
        if raw[o : o + 4] != b"name":
            continue
        _c, toff, tlen = struct.unpack_from(">III", raw, o + 4)
        name = raw[toff : toff + tlen]
        if len(name) < 6:
            return False
        _fmt, count, stroff = struct.unpack_from(">HHH", name, 0)
        for r in range(count):
            rec_o = 6 + r * 12
            if rec_o + 12 > len(name):
                break
            plat, enc, lang, nid, nlen, noff = struct.unpack_from(">HHHHHH", name, rec_o)
            if plat != 3 or enc != 1 or nid != 1:
                continue
            s = name[stroff + noff : stroff + noff + nlen]
            try:
                txt = s.decode("utf-16-be", "replace").strip()
            except Exception:  # noqa: BLE001
                continue
            if txt == want:
                seen.add(lang)
        break
    return 0x0409 in seen and 0x0804 in seen


_FONT_NAME_PATS = [
    re.compile(r"Font\.default_name\s*=\s*\(?\s*\[([^\]]+)\]", re.I),
    re.compile(r'Font\.default_name\s*=\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'Font\.exist\?\s*\(\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'AddFontResource(?:A|W)?\s*\(\s*["\']([^"\']+)["\']', re.I),
]


def _add_font_name(found: List[str], name: str) -> None:
    n = (name or "").strip()
    if not n or n in found:
        return
    if n.lower() in ("true", "false", "nil", "null"):
        return
    found.append(n)


def _font_names_from_script_text(text: str, found: List[str]) -> None:
    for i, pat in enumerate(_FONT_NAME_PATS):
        for m in pat.finditer(text):
            g = m.group(1)
            if i == 0:
                for one in re.findall(r'["\']([^"\']+)["\']', g):
                    _add_font_name(found, one)
            else:
                _add_font_name(found, g)


def _font_names_from_script_blob(scripts: bytes) -> List[str]:
    import zlib

    found: List[str] = []
    if not scripts:
        return found
    i = 0
    n = len(scripts)
    while i < n - 2:
        if scripts[i] == 0x78 and scripts[i + 1] in (0x9C, 0xDA, 0x01, 0x5E):
            try:
                blob = zlib.decompress(scripts[i:])
            except Exception:
                i += 1
                continue
            text = blob.decode("utf-8", "replace")
            if "Font" in text or "AddFont" in text or "字体" in text:
                _font_names_from_script_text(text, found)
                if not found:
                    try:
                        _font_names_from_script_text(blob.decode("gbk", "replace"), found)
                    except Exception:  # noqa: BLE001
                        pass
            i += 1
            continue
        i += 1
    return found


def _decrypt_rgssad_v1_scripts(data: bytes) -> bytes:
    """RGSSAD v1（XP / VX 的 .rgssad / .rgss2a）。"""
    key = 0xDEADCAFE
    off = 8
    while off + 8 <= len(data):
        namelen = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        key = (key * 7 + 3) & 0xFFFFFFFF
        if namelen <= 0 or namelen > 1024 or off + namelen + 4 > len(data):
            break
        nb = bytearray(data[off : off + namelen])
        off += namelen
        for i in range(namelen):
            nb[i] ^= key & 0xFF
            key = (key * 7 + 3) & 0xFFFFFFFF
        size = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        key = (key * 7 + 3) & 0xFFFFFFFF
        name = bytes(nb).decode("utf-8", "replace").replace("\\", "/").lower()
        if name.endswith(("scripts.rxdata", "scripts.rvdata", "scripts.rvdata2")):
            chunk = bytearray(data[off : off + size])
            temp = key
            kb = struct.pack("<I", temp)
            j = 0
            for i in range(len(chunk)):
                if j == 4:
                    j = 0
                    temp = (temp * 7 + 3) & 0xFFFFFFFF
                    kb = struct.pack("<I", temp)
                chunk[i] ^= kb[j]
                j += 1
            return bytes(chunk)
        off += size
    return b""


def _decrypt_rgssad_v3_scripts(data: bytes) -> bytes:
    key0 = struct.unpack_from("<I", data, 8)[0]
    key = (key0 * 9 + 3) & 0xFFFFFFFF
    off = 12
    while off + 16 <= len(data):
        offset = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        if offset == 0:
            break
        size = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        fkey = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        namelen = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        if namelen <= 0 or namelen > 1024 or off + namelen > len(data):
            break
        nb = bytearray(data[off : off + namelen])
        off += namelen
        k = key
        for i in range(namelen):
            nb[i] ^= k & 0xFF
            k = ((k >> 8) | ((k & 0xFF) << 24)) & 0xFFFFFFFF
        name = nb.decode("utf-8", "replace").replace("\\", "/")
        if name.lower().endswith(("scripts.rvdata2", "scripts.rvdata", "scripts.rxdata")):
            chunk = bytearray(data[offset : offset + size])
            temp = fkey
            kb = struct.pack("<I", temp)
            j = 0
            for i in range(len(chunk)):
                if j == 4:
                    j = 0
                    temp = (temp * 7 + 3) & 0xFFFFFFFF
                    kb = struct.pack("<I", temp)
                chunk[i] ^= kb[j]
                j += 1
            return bytes(chunk)
    return b""


def detect_rgss_default_font_names(game_dir: str) -> List[str]:
    """从 RGSS 封包 / 解包 Scripts 里抽出 Font.default_name、Font.exist?。"""
    found: List[str] = []
    if not game_dir or not os.path.isdir(game_dir):
        return found
    archives: List[str] = []
    unpacked: List[str] = []
    try:
        for fn in os.listdir(game_dir):
            low = fn.lower()
            p = os.path.join(game_dir, fn)
            if low.endswith((".rgss3a", ".rgssad", ".rgss2a")) and os.path.isfile(p):
                archives.append(p)
        data_dir = os.path.join(game_dir, "Data")
        if os.path.isdir(data_dir):
            for fn in os.listdir(data_dir):
                low = fn.lower()
                if low in ("scripts.rxdata", "scripts.rvdata", "scripts.rvdata2"):
                    unpacked.append(os.path.join(data_dir, fn))
    except Exception:  # noqa: BLE001
        return found

    blobs: List[bytes] = []
    for archive in archives:
        try:
            data = open(archive, "rb").read()
        except Exception:  # noqa: BLE001
            continue
        if data[:6] != b"RGSSAD" or len(data) < 12:
            continue
        ver = data[7] if len(data) > 7 else 0
        try:
            if ver == 3:
                blobs.append(_decrypt_rgssad_v3_scripts(data))
            else:
                blobs.append(_decrypt_rgssad_v1_scripts(data))
        except Exception:  # noqa: BLE001
            continue
    for path in unpacked:
        try:
            blobs.append(open(path, "rb").read())
        except Exception:  # noqa: BLE001
            continue
    for blob in blobs:
        for name in _font_names_from_script_blob(blob):
            _add_font_name(found, name)
    return found


CJK_FONT_SIZE_OPTIONS: List[Dict[str, Any]] = [
    {"id": 0, "label": "不改字号"},
    {"id": 24, "label": "24（标准）"},
    {"id": 28, "label": "28（稍大）"},
    {"id": 32, "label": "32（更大）"},
]


def resolve_cjk_font_size(raw: Any) -> int:
    """0 = 不改。合法范围 8–48。"""
    if raw is None or raw == "":
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return max(8, min(48, n))


def _marshal_dump_long(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    if 1 <= n <= 122:
        return bytes([n + 5])
    if n < 0:
        raise ValueError("negative marshal long")
    buf = bytearray()
    x = n
    while x:
        buf.append(x & 0xFF)
        x >>= 8
    if buf[-1] & 0x80:
        buf.append(0)
    if len(buf) > 4:
        raise ValueError("marshal long too big")
    return bytes([len(buf)]) + bytes(buf)


def _marshal_load_long(buf: bytes, i: int) -> tuple:
    if i >= len(buf):
        raise ValueError("eof")
    c = buf[i]
    if c == 0:
        return 0, i + 1
    if 6 <= c <= 127:
        return c - 5, i + 1
    if 1 <= c <= 4:
        n = 0
        for k in range(c):
            n |= buf[i + 1 + k] << (8 * k)
        return n, i + 1 + c
    raise ValueError("not a positive marshal long")


def _iter_zlib_scripts(scripts: bytes):
    import zlib

    i = 0
    n = len(scripts)
    while i < n - 2:
        if scripts[i] == 0x78 and scripts[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            d = zlib.decompressobj()
            try:
                plain = d.decompress(scripts[i:])
            except Exception:
                i += 1
                continue
            zlen = len(scripts[i:]) - len(d.unused_data)
            if zlen > 8:
                yield i, zlen, plain
            i += 1
            continue
        i += 1


_FONT_SIZE_ASSIGN = re.compile(r"(Font\.default_size\s*=\s*)(\d+)", re.I)


def detect_rgss_default_font_size(scripts: bytes) -> Optional[int]:
    for _off, _zlen, plain in _iter_zlib_scripts(scripts):
        text = plain.decode("utf-8", "replace")
        m = _FONT_SIZE_ASSIGN.search(text)
        if m:
            try:
                return int(m.group(2))
            except ValueError:
                continue
    return None


def _replace_rgss_font_size_in_scripts(scripts: bytes, new_size: int) -> tuple:
    import zlib

    out = bytearray(scripts)
    old_size = detect_rgss_default_font_size(scripts)
    replaced = 0
    # 从后往前改，避免偏移错位
    hits = []
    for zoff, zlen, plain in _iter_zlib_scripts(scripts):
        text = plain.decode("utf-8", "replace")
        if not _FONT_SIZE_ASSIGN.search(text):
            continue
        text2, n = _FONT_SIZE_ASSIGN.subn(rf"\g<1>{new_size}", text)
        if n:
            hits.append((zoff, zlen, text2.encode("utf-8")))
    for zoff, zlen, new_plain in reversed(hits):
        new_z = zlib.compress(new_plain, 9)
        start = -1
        for nlen in range(1, 6):
            s = zoff - nlen
            if s < 1:
                continue
            try:
                val, end = _marshal_load_long(out, s)
            except Exception:
                continue
            if end == zoff and val == zlen:
                start = s
                break
        if start < 0:
            continue
        new_lenb = _marshal_dump_long(len(new_z))
        out = out[:start] + new_lenb + new_z + out[zoff + zlen :]
        replaced += 1
    return bytes(out), {"old": old_size, "new": new_size, "replaced": replaced}


def _game_ini_scripts_rel(game_dir: str) -> str:
    for name in ("Game.ini", "game.ini"):
        p = os.path.join(game_dir, name)
        if not os.path.isfile(p):
            continue
        raw = open(p, "rb").read()
        text = ""
        for enc in ("utf-8", "utf-16", "gbk", "shift_jis", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        m = re.search(r"(?im)^\s*Scripts\s*=\s*(.+)$", text)
        if m:
            rel = m.group(1).strip().strip('"').replace("\\", "/")
            if rel:
                return rel
    return "Data/Scripts.rvdata2"


def _find_rgss_archive(game_dir: str) -> str:
    try:
        for fn in os.listdir(game_dir):
            low = fn.lower()
            if low.endswith((".rgss3a", ".rgssad", ".rgss2a")):
                return os.path.join(game_dir, fn)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _load_rgss_scripts_blob(game_dir: str) -> tuple:
    """返回 (blob, loose_write_path, archive_path_or_empty)。优先磁盘 Scripts。"""
    rel = _game_ini_scripts_rel(game_dir)
    loose = os.path.join(game_dir, rel)
    archive = _find_rgss_archive(game_dir)
    v3_archive = ""
    if archive and os.path.isfile(archive):
        try:
            head = open(archive, "rb").read(8)
            if head[:6] == b"RGSSAD" and len(head) > 7 and head[7] == 3:
                v3_archive = archive
        except Exception:  # noqa: BLE001
            v3_archive = ""
    if os.path.isfile(loose) and os.path.getsize(loose) > 64:
        return open(loose, "rb").read(), loose, v3_archive
    # 常见文件名
    for cand in (
        os.path.join(game_dir, "Data", "Scripts.rvdata2"),
        os.path.join(game_dir, "Data", "Scripts.rvdata"),
        os.path.join(game_dir, "Data", "Scripts.rxdata"),
    ):
        if os.path.isfile(cand) and os.path.getsize(cand) > 64:
            return open(cand, "rb").read(), cand, v3_archive
    if not archive or not os.path.isfile(archive):
        return b"", "", ""
    data = open(archive, "rb").read()
    if data[:6] != b"RGSSAD" or len(data) < 12:
        return b"", "", ""
    ver = data[7]
    blob = _decrypt_rgssad_v3_scripts(data) if ver == 3 else _decrypt_rgssad_v1_scripts(data)
    if not blob:
        return b"", "", ""
    write_name = "Scripts.rvdata2" if ver == 3 else ("Scripts.rvdata" if ver == 1 else "Scripts.rxdata")
    write_path = os.path.join(game_dir, rel) if rel else os.path.join(game_dir, "Data", write_name)
    return blob, write_path, v3_archive


def _rgssad_v3_replace_file(archive: str, inner_name: str, new_bytes: bytes) -> str:
    """覆盖 RGSSAD v3 里的某个文件；新内容不能比原槽位更大。"""
    data = bytearray(open(archive, "rb").read())
    if data[:6] != b"RGSSAD" or data[7] != 3:
        return "not-v3"
    key0 = struct.unpack_from("<I", data, 8)[0]
    key = (key0 * 9 + 3) & 0xFFFFFFFF
    off = 12
    want = inner_name.replace("\\", "/").lower()
    while off + 16 <= len(data):
        rec = off
        e_off = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        if e_off == 0:
            break
        e_size = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        e_fkey = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        e_nlen = struct.unpack_from("<I", data, off)[0] ^ key
        off += 4
        if e_nlen <= 0 or e_nlen > 1024 or off + e_nlen > len(data):
            break
        nb = bytearray(data[off : off + e_nlen])
        off += e_nlen
        k = key
        for i in range(e_nlen):
            nb[i] ^= k & 0xFF
            k = ((k >> 8) | ((k & 0xFF) << 24)) & 0xFFFFFFFF
        name = bytes(nb).decode("utf-8", "replace").replace("\\", "/").lower()
        if name != want and not name.endswith("/" + want) and os.path.basename(name) != os.path.basename(want):
            continue
        if len(new_bytes) > e_size:
            return "too-big"
        enc = bytearray(new_bytes)
        temp = e_fkey
        kb = struct.pack("<I", temp)
        j = 0
        for i in range(len(enc)):
            if j == 4:
                j = 0
                temp = (temp * 7 + 3) & 0xFFFFFFFF
                kb = struct.pack("<I", temp)
            enc[i] ^= kb[j]
            j += 1
        data[e_off : e_off + len(enc)] = enc
        struct.pack_into("<I", data, rec + 4, len(new_bytes) ^ key)
        bak = archive + ".bak_nsc_fontsize"
        if not os.path.isfile(bak):
            # 太大时只备份 Scripts 由调用方处理
            pass
        with open(archive, "wb") as fp:
            fp.write(data)
        return f"archive:{name}:{e_size}->{len(new_bytes)}"
    return "not-found"


def patch_rgss_font_size(game_dir: str, new_size: int) -> Dict[str, Any]:
    """把 Font.default_size 改成 new_size。写 Data/Scripts.*，Ace 同时改封包。"""
    out: Dict[str, Any] = {"ok": False, "changes": [], "errors": [], "old": None, "new": new_size}
    blob, loose, archive = _load_rgss_scripts_blob(game_dir)
    if not blob:
        out["errors"].append("无 Scripts")
        return out
    old = detect_rgss_default_font_size(blob)
    out["old"] = old
    if old == new_size:
        out["ok"] = True
        out["changes"].append(f"already:{old}")
        return out
    new_blob, info = _replace_rgss_font_size_in_scripts(blob, new_size)
    if not info.get("replaced"):
        out["errors"].append("脚本里没有 Font.default_size")
        return out
    try:
        os.makedirs(os.path.dirname(loose), exist_ok=True)
        bak = loose + ".bak_nsc_size"
        if os.path.isfile(loose) and not os.path.isfile(bak):
            shutil.copy2(loose, bak)
        elif not os.path.isfile(bak):
            with open(bak, "wb") as fp:
                fp.write(blob)
        with open(loose, "wb") as fp:
            fp.write(new_blob)
        out["changes"].append(f"scripts:{old}->{new_size}")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"write:{e}")
        return out
    if archive:
        inner = os.path.relpath(loose, game_dir).replace(os.sep, "/")
        try:
            r = _rgssad_v3_replace_file(archive, inner, new_blob)
            out["changes"].append(r)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"archive:{e}")
    out["ok"] = True
    out["old"] = info.get("old", old)
    return out


def patch_html_maker_font_size(root: str, new_size: int) -> Dict[str, Any]:
    """MZ System.json advanced.fontSize；MV YEP CoreEngine Font Size。"""
    out: Dict[str, Any] = {"ok": False, "changes": [], "errors": []}
    import json

    for cand in (
        os.path.join(root, "data", "System.json"),
        os.path.join(root, "www", "data", "System.json"),
    ):
        if not os.path.isfile(cand):
            continue
        try:
            sysj = json.load(open(cand, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"System.json:{e}")
            continue
        adv = sysj.get("advanced")
        if not isinstance(adv, dict) or "fontSize" not in adv:
            continue
        old = adv.get("fontSize")
        if old == new_size:
            out["changes"].append(f"System.json:already:{old}")
            out["ok"] = True
            continue
        adv["fontSize"] = new_size
        sysj["advanced"] = adv
        bak = cand + ".bak_nsc_size"
        try:
            if not os.path.isfile(bak):
                shutil.copy2(cand, bak)
            json.dump(sysj, open(cand, "w", encoding="utf-8"), ensure_ascii=False)
            out["changes"].append(f"System.json:{old}->{new_size}")
            out["ok"] = True
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"System.json:{e}")

    for cand in (
        os.path.join(root, "js", "plugins.js"),
        os.path.join(root, "www", "js", "plugins.js"),
    ):
        if not os.path.isfile(cand):
            continue
        try:
            text = open(cand, encoding="utf-8", errors="replace").read()
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"plugins.js:{e}")
            continue
        if '"Font Size"' not in text:
            continue
        new_text, n = re.subn(
            r'("Font Size"\s*:\s*")(\d+)(")',
            rf"\g<1>{new_size}\3",
            text,
            count=1,
        )
        if not n or new_text == text:
            continue
        bak = cand + ".bak_nsc_size"
        try:
            if not os.path.isfile(bak):
                shutil.copy2(cand, bak)
            open(cand, "w", encoding="utf-8").write(new_text)
            out["changes"].append(f"YEP Font Size->{new_size}")
            out["ok"] = True
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"plugins.js:{e}")
    if not out["changes"] and not out["errors"]:
        out["errors"].append("无 MZ/YEP 字号字段")
    return out


def patch_game_font_size(start_dir: str = "", exe: str = "", font_size: Any = 0) -> Dict[str, Any]:
    """按游戏类型加大默认字号（RPG Maker）。"""
    size = resolve_cjk_font_size(font_size)
    out: Dict[str, Any] = {
        "ok": False,
        "skipped": False,
        "size": size,
        "changes": [],
        "errors": [],
    }
    if size <= 0:
        out["ok"] = True
        out["skipped"] = True
        return out
    roots = _game_roots(start_dir, exe)
    if not roots:
        out["errors"].append("无游戏目录")
        return out
    for root in roots:
        if _is_rgss_root(root):
            r = patch_rgss_font_size(root, size)
            out["changes"].extend(r.get("changes") or [])
            out["errors"].extend(r.get("errors") or [])
            if r.get("ok"):
                out["ok"] = True
                out["old"] = r.get("old")
        if _is_html_maker_root(root):
            r = patch_html_maker_font_size(root, size)
            out["changes"].extend(r.get("changes") or [])
            out["errors"].extend(r.get("errors") or [])
            if r.get("ok"):
                out["ok"] = True
    if not out["changes"] and not out["ok"]:
        if not out["errors"]:
            out["errors"].append("该游戏没有可改的默认字号")
    return out


def _restore_bak_if_replaced(path: str) -> bool:
    bak = path + ".bak_nsc"
    if not os.path.isfile(bak):
        return False
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        shutil.copy2(bak, path)
        return True
    except Exception:  # noqa: BLE001
        return False


_SKIP_FONT_WALK = {
    "node_modules",
    "locales",
    "dictionaries",
    "img",
    "audio",
    "effects",
    "graphics",
    "save",
    "movies",
    "live2d",
    "swiftshader",
    "mtool",
    "pnacl",
    "__pycache__",
    ".git",
    "dataex",
}

_BROAD_FONT_ROOTS = {
    "/",
    "/home",
    "/home/deck",
    "/usr",
    "/opt",
    "/var",
    "/tmp",
    os.path.expanduser("~"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Applications"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Games"),
    os.path.expanduser("~/Emulation"),
    "/home/deck/Downloads",
    "/home/deck/Downloads/installed",
    "/home/deck/Applications",
    "/home/deck/Desktop",
    "/home/deck/Games",
    "/home/deck/Emulation",
}


def _norm_game_path(raw: str) -> str:
    p = os.path.expanduser(str(raw or "").strip().strip('"'))
    p = p.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = p[2:]
    return p


def _is_too_broad_root(path: str) -> bool:
    if not path:
        return True
    rp = os.path.realpath(path)
    if rp in _BROAD_FONT_ROOTS:
        return True
    return rp.rstrip("/") in {p.rstrip("/") for p in _BROAD_FONT_ROOTS}


def _game_roots(start_dir: str = "", exe: str = "") -> List[str]:
    bases: List[str] = []
    for raw in (start_dir, os.path.dirname(exe) if exe else ""):
        p = _norm_game_path(raw)
        if p and os.path.isdir(p) and not _is_too_broad_root(p):
            rp = os.path.realpath(p)
            if rp not in bases:
                bases.append(rp)
    extra: List[str] = []
    for base in bases:
        for sub in ("www", "game", "Game"):
            cand = os.path.join(base, sub)
            if os.path.isdir(cand) and not _is_too_broad_root(cand):
                rp = os.path.realpath(cand)
                if rp not in bases and rp not in extra:
                    extra.append(rp)
        bin_www = os.path.join(base, "bin", "www")
        if os.path.isdir(bin_www) and not _is_too_broad_root(bin_www):
            rp = os.path.realpath(bin_www)
            if rp not in bases and rp not in extra:
                extra.append(rp)
    return bases + extra


def find_font_dirs(start_dir: str = "", exe: str = "") -> List[str]:
    """只在游戏目录内找 RPG Maker Fonts / www/fonts，绝不扫 Downloads 整盘。"""
    found: List[str] = []
    rels = (
        "Fonts",
        "fonts",
        "Font",
        "font",
        os.path.join("www", "fonts"),
        os.path.join("www", "Fonts"),
        os.path.join("bin", "www", "fonts"),
        os.path.join("game", "fonts"),
        os.path.join("game", "www", "fonts"),
    )
    for root in _game_roots(start_dir, exe):
        if _is_too_broad_root(root):
            continue
        for rel in rels:
            cand = os.path.join(root, rel)
            if os.path.isdir(cand):
                rp = os.path.realpath(cand)
                if rp not in found:
                    found.append(rp)
    return found


def _is_rgss_root(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        names = [n.lower() for n in os.listdir(path)]
    except Exception:  # noqa: BLE001
        return False
    if any(n.endswith((".rgss3a", ".rgssad", ".rgss2a")) for n in names):
        return True
    if "game.ini" in names and any(n.endswith(".dll") and "rgss" in n for n in names):
        return True
    return "game.ini" in names and any("rgss" in n for n in names)


def _is_html_maker_root(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    if os.path.isfile(os.path.join(path, "js", "rpg_core.js")):
        return True
    if os.path.isfile(os.path.join(path, "js", "rmmz_core.js")):
        return True
    if os.path.isfile(os.path.join(path, "www", "js", "rpg_core.js")):
        return True
    if os.path.isfile(os.path.join(path, "www", "js", "rmmz_core.js")):
        return True
    return os.path.isfile(os.path.join(path, "fonts", "gamefont.css")) or os.path.isfile(
        os.path.join(path, "www", "fonts", "gamefont.css")
    )


def _css_font_families(spec: str) -> List[str]:
    skip = {"sans-serif", "serif", "monospace", "cursive", "fantasy", "system-ui"}
    out: List[str] = []
    for part in (spec or "").split(","):
        name = part.strip().strip("'\"").strip()
        if name and name.lower() not in skip and name not in out:
            out.append(name)
    return out


def detect_html_maker_fonts(root: str) -> Dict[str, Any]:
    """MV/MZ：System.json 字体文件、gamefont.css、YEP Chinese Font。"""
    info: Dict[str, Any] = {
        "files": [],
        "families": [],
        "css_paths": [],
        "system_jsons": [],
    }
    if not root or not os.path.isdir(root):
        return info

    def add_file(name: str) -> None:
        n = (name or "").strip().lstrip("/").replace("\\", "/")
        if n.startswith("fonts/"):
            n = n[6:]
        if n and n not in info["files"]:
            info["files"].append(n)

    def add_fam(name: str) -> None:
        for one in _css_font_families(name):
            if one not in info["families"]:
                info["families"].append(one)

    for cand in (
        os.path.join(root, "data", "System.json"),
        os.path.join(root, "www", "data", "System.json"),
    ):
        if not os.path.isfile(cand):
            continue
        info["system_jsons"].append(cand)
        try:
            import json

            sysj = json.load(open(cand, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        adv = sysj.get("advanced") or {}
        for key in ("mainFontFilename", "numberFontFilename"):
            add_file(str(adv.get(key) or ""))
        add_fam(str(adv.get("fallbackFonts") or ""))

    for cand in (
        os.path.join(root, "fonts", "gamefont.css"),
        os.path.join(root, "www", "fonts", "gamefont.css"),
        os.path.join(root, "css", "game.css"),
        os.path.join(root, "www", "css", "game.css"),
    ):
        if not os.path.isfile(cand):
            continue
        info["css_paths"].append(cand)
        try:
            text = open(cand, encoding="utf-8", errors="replace").read()
        except Exception:  # noqa: BLE001
            continue
        for m in re.finditer(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", text, re.I):
            add_file(os.path.basename(m.group(1)))
        for m in re.finditer(r"font-family\s*:\s*([^;{]+)", text, re.I):
            add_fam(m.group(1))

    for cand in (
        os.path.join(root, "js", "plugins.js"),
        os.path.join(root, "www", "js", "plugins.js"),
    ):
        if not os.path.isfile(cand):
            continue
        try:
            text = open(cand, encoding="utf-8", errors="replace").read()
        except Exception:  # noqa: BLE001
            continue
        for key in ("Chinese Font", "Korean Font", "Default Font"):
            m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]+)"', text)
            if m:
                add_fam(m.group(1))
    return info


def detect_families_from_font_files(fonts_dir: str) -> List[str]:
    """用已有字体文件名/族名兜底（例如 萝莉体.ttf）。"""
    found: List[str] = []
    if not fonts_dir or not os.path.isdir(fonts_dir):
        return found
    skip_stem = {
        "msyh",
        "simhei",
        "simsun",
        "minglan",
        "minglan",
        "vl-gothic-regular",
        "vl-pgothic-regular",
        "vl gothic",
        "umeplus-gothic",
        "nsc-cjk",
        "mplus-1m-regular",
        "mplus-2p-bold-sub",
        "gamefont",
    }
    try:
        names = os.listdir(fonts_dir)
    except Exception:  # noqa: BLE001
        return found
    for fn in names:
        low = fn.lower()
        if not low.endswith((".ttf", ".otf", ".ttc")):
            continue
        stem = os.path.splitext(fn)[0]
        if stem.lower() in skip_stem:
            continue
        if any("\u4e00" <= ch <= "\u9fff" for ch in stem):
            _add_font_name(found, stem)
        path = os.path.join(fonts_dir, fn)
        if os.path.islink(path):
            continue
        for fam in read_ttf_family_names(path):
            if fam.lower() in skip_stem:
                continue
            if any("\u4e00" <= ch <= "\u9fff" for ch in fam) or fam in (
                "MINGLAN",
                "MingLan",
            ):
                _add_font_name(found, fam)
    return found


def _safe_font_filename(family: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in family).strip()
    return (safe or "Custom") + ".ttf"


def _find_existing_font_for_family(fonts_dir: str, family: str) -> str:
    exact = os.path.join(fonts_dir, family + ".ttf")
    if os.path.isfile(exact):
        return exact
    safe = os.path.join(fonts_dir, _safe_font_filename(family))
    if os.path.isfile(safe):
        return safe
    try:
        for fn in os.listdir(fonts_dir):
            p = os.path.join(fonts_dir, fn)
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            low = fn.lower()
            if not low.endswith((".ttf", ".otf")):
                continue
            if os.path.splitext(fn)[0] == family:
                return p
            if ttf_has_family(p, family):
                return p
    except Exception:  # noqa: BLE001
        pass
    return ""


def _vl_family_ok(path: str, want: str) -> bool:
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    try:
        if os.path.getsize(path) < 200_000:
            return False
    except Exception:  # noqa: BLE001
        return False
    names = read_ttf_family_names(path)
    if not names:
        return False
    return any(want == n or want in n or n in want for n in names)


def _find_good_vl_source(fname: str) -> str:
    home = os.path.expanduser("~")
    want = "VL PGothic" if "PGothic" in fname or "pgothic" in fname.lower() else "VL Gothic"
    candidates = [
        os.path.join(home, "Downloads/KingExit/Fonts", fname),
        os.path.join("/home/deck/Downloads/KingExit/Fonts", fname),
    ]
    for c in candidates:
        if _vl_family_ok(c, want):
            return c
    return ""


def restore_vl_gothic(fonts_dir: str) -> List[str]:
    done: List[str] = []
    for vl, want in (
        ("VL-Gothic-Regular.ttf", "VL Gothic"),
        ("VL-PGothic-Regular.ttf", "VL PGothic"),
    ):
        path = os.path.join(fonts_dir, vl)
        if _vl_family_ok(path, want):
            continue
        if _restore_bak_if_replaced(path) and _vl_family_ok(path, want):
            done.append(f"{vl}:restored")
            continue
        src = _find_good_vl_source(vl)
        if src:
            try:
                if os.path.lexists(path):
                    os.remove(path)
                shutil.copy2(src, path)
                if not ttf_family_visible_to_rgss(path, want):
                    clone_ttf_with_family(path, path, want)
                done.append(f"{vl}:copied")
                continue
            except Exception as e:  # noqa: BLE001
                done.append(f"{vl}:copy-error:{e}")
        ume = os.path.join(fonts_dir, "umeplus-gothic.ttf")
        if os.path.isfile(ume) and not os.path.islink(ume):
            r = clone_ttf_with_family(ume, path, want)
            done.append(f"{vl}:{r}")
            continue
        src = _pick_cjk_ttf_source()
        if src:
            r = clone_ttf_with_family(src, path, want)
            done.append(f"{vl}:{r}")
    return done


def _ensure_family_ttf(fonts_dir: str, family: str, src: str) -> str:
    """保证 fonts_dir 里有族名为 family 的真实 TTF。优先改名已有文件，不覆盖正主。"""
    dest = _find_existing_font_for_family(fonts_dir, family)
    if dest and os.path.isfile(dest) and not os.path.islink(dest):
        if ttf_family_visible_to_rgss(dest, family):
            return f"{os.path.basename(dest)}:has:{family}"
        r = clone_ttf_with_family(dest, dest, family)
        return f"{os.path.basename(dest)}:{r}"
    dest = os.path.join(fonts_dir, _safe_font_filename(family))
    if os.path.islink(dest):
        try:
            os.remove(dest)
        except Exception:  # noqa: BLE001
            pass
    r = clone_ttf_with_family(src, dest, family)
    return f"{os.path.basename(dest)}:{r}"


def _patch_gamefont_css(css_path: str, cjk_file: str = "nsc-cjk.ttf") -> str:
    try:
        text = open(css_path, encoding="utf-8", errors="replace").read()
    except Exception as e:  # noqa: BLE001
        return f"error:{e}"
    if "nsc-cjk" in text and "unicode-range" in text:
        return "unchanged"
    href = cjk_file
    if os.path.isabs(cjk_file):
        href = os.path.relpath(cjk_file, os.path.dirname(css_path)).replace("\\", "/")
    extra = (
        "\n/* NonSteamCleaner CJK overlay */\n"
        "@font-face {\n"
        "    font-family: GameFont;\n"
        f'    src: url("{href}");\n'
        "    unicode-range: U+4E00-9FFF, U+3400-4DBF, U+F900-FAFF, U+3000-303F, U+FF00-FFEF;\n"
        "}\n"
        "@font-face {\n"
        "    font-family: gamefont;\n"
        f'    src: url("{href}");\n'
        "    unicode-range: U+4E00-9FFF, U+3400-4DBF, U+F900-FAFF, U+3000-303F, U+FF00-FFEF;\n"
        "}\n"
        "@font-face {\n"
        "    font-family: SimHei;\n"
        f'    src: url("{href}");\n'
        "}\n"
        "@font-face {\n"
        "    font-family: \"Heiti TC\";\n"
        f'    src: url("{href}");\n'
        "}\n"
        "@font-face {\n"
        "    font-family: 黑体;\n"
        f'    src: url("{href}");\n'
        "}\n"
    )
    bak = css_path + ".bak_nsc"
    try:
        if not os.path.isfile(bak):
            shutil.copy2(css_path, bak)
        with open(css_path, "a", encoding="utf-8") as fp:
            fp.write(extra)
        return "patched"
    except Exception as e:  # noqa: BLE001
        return f"error:{e}"


def _ensure_system_json_fallback(path: str) -> str:
    try:
        import json

        raw = open(path, encoding="utf-8").read()
        sysj = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return f"error:{e}"
    adv = sysj.get("advanced")
    if not isinstance(adv, dict):
        return "no-advanced"
    fb = str(adv.get("fallbackFonts") or "")
    if "SimHei" in fb or "nsc-cjk" in fb:
        return "unchanged"
    adv["fallbackFonts"] = ("SimHei, nsc-cjk, " + fb).strip().rstrip(",")
    sysj["advanced"] = adv
    bak = path + ".bak_nsc"
    try:
        if not os.path.isfile(bak):
            shutil.copy2(path, bak)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(sysj, fp, ensure_ascii=False)
        return "fallback"
    except Exception as e:  # noqa: BLE001
        return f"error:{e}"


def _ensure_referenced_font_file(fonts_dir: str, filename: str, src: str, system_jsons: List[str]) -> str:
    dest = os.path.join(fonts_dir, filename)
    if os.path.isfile(dest) and not os.path.islink(dest) and os.path.getsize(dest) > 2048:
        return f"{filename}:exists"
    stem, ext = os.path.splitext(filename)
    try:
        for fn in os.listdir(fonts_dir):
            if os.path.splitext(fn)[0] == stem and fn.lower() != filename.lower():
                alt = os.path.join(fonts_dir, fn)
                if os.path.isfile(alt) and os.path.getsize(alt) > 2048:
                    if ext.lower() == os.path.splitext(fn)[1].lower():
                        shutil.copy2(alt, dest)
                        return f"{filename}:copied-alt"
                    # MZ 要 .woff 但只有 .ttf：改 System.json 指向现有文件
                    for sj in system_jsons:
                        try:
                            import json

                            sysj = json.load(open(sj, encoding="utf-8"))
                            adv = sysj.get("advanced") or {}
                            dirty = False
                            for key in ("mainFontFilename", "numberFontFilename"):
                                if str(adv.get(key) or "") == filename:
                                    adv[key] = fn
                                    dirty = True
                            if dirty:
                                bak = sj + ".bak_nsc"
                                if not os.path.isfile(bak):
                                    shutil.copy2(sj, bak)
                                sysj["advanced"] = adv
                                json.dump(sysj, open(sj, "w", encoding="utf-8"), ensure_ascii=False)
                                return f"{filename}:retarget->{fn}"
                        except Exception:  # noqa: BLE001
                            continue
    except Exception:  # noqa: BLE001
        pass
    if ext.lower() in (".ttf", ".otf"):
        r = clone_ttf_with_family(src, dest, stem)
        return f"{filename}:{r}"
    # woff 缺失：写同名 ttf 并改 System.json
    ttf_dest = os.path.join(fonts_dir, stem + ".ttf")
    r = clone_ttf_with_family(src, ttf_dest, stem)
    for sj in system_jsons:
        try:
            import json

            sysj = json.load(open(sj, encoding="utf-8"))
            adv = sysj.get("advanced") or {}
            dirty = False
            for key in ("mainFontFilename", "numberFontFilename"):
                if str(adv.get(key) or "") == filename:
                    adv[key] = stem + ".ttf"
                    dirty = True
            if dirty:
                bak = sj + ".bak_nsc"
                if not os.path.isfile(bak):
                    shutil.copy2(sj, bak)
                sysj["advanced"] = adv
                json.dump(sysj, open(sj, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            continue
    return f"{filename}:as-ttf:{r}"


def install_game_local_cjk_fonts(start_dir: str = "", exe: str = "", preset: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """给 RPG Maker 等「只读游戏目录 Fonts/」的引擎补中文字体。

    覆盖 VX/Ace（Fonts + 族名）、MV/MZ（www/fonts + GameFont/SimHei）。
    """
    preset = preset or {}
    out: Dict[str, Any] = {
        "ok": False,
        "dir": "",
        "dirs": [],
        "changes": [],
        "errors": [],
        "detected_fonts": [],
        "prefix_families": [],
    }
    roots = _game_roots(start_dir, exe)
    font_dirs = find_font_dirs(start_dir, exe)

    detected: List[str] = []
    html: Dict[str, Any] = {"files": [], "families": [], "css_paths": [], "system_jsons": []}
    for root in roots:
        for fam in detect_rgss_default_font_names(root):
            _add_font_name(detected, fam)
        info = detect_html_maker_fonts(root)
        for k in ("files", "families", "css_paths", "system_jsons"):
            for item in info.get(k) or []:
                if item not in html[k]:
                    html[k].append(item)

    if not font_dirs:
        for root in roots:
            if _is_rgss_root(root):
                fd = os.path.join(root, "Fonts")
                try:
                    os.makedirs(fd, exist_ok=True)
                    font_dirs.append(fd)
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"mkdir Fonts:{e}")
            elif _is_html_maker_root(root):
                fd = (
                    os.path.join(root, "www", "fonts")
                    if os.path.isdir(os.path.join(root, "www"))
                    else os.path.join(root, "fonts")
                )
                try:
                    os.makedirs(fd, exist_ok=True)
                    font_dirs.append(os.path.realpath(fd))
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"mkdir fonts:{e}")

    if not font_dirs:
        out["errors"].append("无游戏 Fonts 目录（非 RPG Maker 类可忽略）")
        return out

    src = _pick_cjk_ttf_source()
    if not src:
        out["errors"].append("找不到可用中文 TTF（msyh/Noto）")
        return out

    out["dirs"] = font_dirs
    out["dir"] = font_dirs[0]
    prefix_fams: List[tuple] = []

    for fonts_dir in font_dirs:
        parent = os.path.dirname(fonts_dir)
        rgss = _is_rgss_root(parent)
        html_here = _is_html_maker_root(parent) or any(
            os.path.isfile(os.path.join(fonts_dir, n))
            for n in ("gamefont.css", "GameFont.css")
        )
        for fam in detect_rgss_default_font_names(parent):
            _add_font_name(detected, fam)
        if rgss:
            for fam in detect_families_from_font_files(fonts_dir):
                _add_font_name(detected, fam)
            for item in restore_vl_gothic(fonts_dir):
                out["changes"].append(item)

        wanted = list(detected) if rgss else []
        if rgss:
            if preset.get("need_heiti") == "1":
                for extra in ("黑体", "SimHei"):
                    if extra not in wanted:
                        wanted.append(extra)
            if not wanted:
                wanted.extend(["黑体", "SimHei", "VL Gothic"])

        for fam in wanted:
            r = _ensure_family_ttf(fonts_dir, fam, src)
            out["changes"].append(r)
            fname = str(r).split(":", 1)[0] if r else _safe_font_filename(fam)
            prefix_fams.append((fam, fname))

        if html_here or html["files"] or html["families"]:
            if preset.get("need_heiti") == "1":
                r = _ensure_family_ttf(fonts_dir, "nsc-cjk", src)
                out["changes"].append(r)
                r2 = _ensure_family_ttf(fonts_dir, "SimHei", src)
                out["changes"].append(r2)
                prefix_fams.append(("SimHei", _safe_font_filename("SimHei")))
                prefix_fams.append(("黑体", _safe_font_filename("黑体")))
                prefix_fams.append(("Heiti TC", _safe_font_filename("Heiti TC")))
                nsc = os.path.join(fonts_dir, "nsc-cjk.ttf")
                if not os.path.isfile(nsc):
                    nsc = os.path.join(fonts_dir, _safe_font_filename("nsc-cjk"))
                if os.path.isfile(nsc):
                    css_targets = list(html["css_paths"])
                    for css in (
                        os.path.join(fonts_dir, "gamefont.css"),
                        os.path.join(fonts_dir, "GameFont.css"),
                    ):
                        if css not in css_targets and os.path.isfile(css):
                            css_targets.append(css)
                    for css in css_targets:
                        out["changes"].append(
                            f"{os.path.basename(css)}:{_patch_gamefont_css(css, nsc)}"
                        )
                for sj in html["system_jsons"]:
                    out["changes"].append(f"{os.path.basename(sj)}:{_ensure_system_json_fallback(sj)}")
            for fname in html["files"]:
                if not fname or fname.lower().endswith(".css"):
                    continue
                out["changes"].append(
                    _ensure_referenced_font_file(fonts_dir, fname, src, html["system_jsons"])
                )

    # 去重 prefix 注册列表，并补常见别名
    seen = set()
    uniq: List[tuple] = []
    for fam, fn in prefix_fams:
        key = (fam, fn)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    if detected:
        for fam in detected:
            fn = _safe_font_filename(fam)
            if (fam, fn) not in seen:
                uniq.append((fam, fn))
                seen.add((fam, fn))
    out["detected_fonts"] = detected
    out["prefix_families"] = [{"family": a, "file": b} for a, b in uniq]
    out["ok"] = bool(out["changes"]) and not any(str(e).startswith("error") for e in out["errors"])
    if out["changes"]:
        out["ok"] = True
        out["source"] = src
    return out


def ensure_simhei_font_links(fonts_dir: str, extra_families: Optional[List[Any]] = None) -> List[str]:
    """在 Windows Fonts 目录写入带正确族名的中文字体。"""
    done: List[str] = []
    if not os.path.isdir(fonts_dir):
        return done
    src = _pick_cjk_ttf_source()
    named = [
        ("simhei.ttf", "SimHei"),
        ("黑体.ttf", "黑体"),
        ("MINGLAN.ttf", "MINGLAN"),
    ]
    for item in extra_families or []:
        if isinstance(item, dict):
            fam = str(item.get("family") or "")
            fn = str(item.get("file") or _safe_font_filename(fam))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            fam, fn = str(item[0]), str(item[1])
        else:
            continue
        if fam:
            named.append((fn, fam))
    if src:
        seen = set()
        for fname, family in named:
            key = (fname.lower(), family)
            if key in seen:
                continue
            seen.add(key)
            dest = os.path.join(fonts_dir, fname)
            if ttf_family_visible_to_rgss(dest, family):
                done.append(f"{fname}=ok")
                continue
            r = clone_ttf_with_family(src, dest, family)
            if r and not str(r).startswith("error"):
                done.append(f"{fname}:{r}")
            else:
                done.append(f"{fname}:{r}")
    return done


def patch_proton_prefix_cjk(
    prefix_dir: str,
    preset: Dict[str, str],
    extra_families: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """修补单个 compatdata 前缀。"""
    result: Dict[str, Any] = {
        "prefix": prefix_dir,
        "ok": False,
        "changes": [],
        "errors": [],
    }
    pfx = os.path.join(prefix_dir, "pfx")
    if not os.path.isdir(pfx):
        if os.path.isdir(os.path.join(prefix_dir, "drive_c")):
            pfx = prefix_dir
        else:
            result["errors"].append("无 pfx 目录（可能尚未启动过游戏）")
            return result

    user_reg = os.path.join(pfx, "user.reg")
    system_reg = os.path.join(pfx, "system.reg")
    fonts_dir = os.path.join(pfx, "drive_c", "windows", "Fonts")

    if preset.get("need_heiti") == "1" or extra_families:
        try:
            os.makedirs(fonts_dir, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        links = ensure_simhei_font_links(fonts_dir, extra_families=extra_families)
        if links:
            result["changes"].append("fonts:" + ",".join(links))

    for reg_path, is_system in ((system_reg, True), (user_reg, False)):
        if not os.path.isfile(reg_path):
            result["errors"].append(f"缺少 {os.path.basename(reg_path)}")
            continue
        try:
            bak = reg_path + ".bak_nsc_cjk"
            if not os.path.isfile(bak):
                shutil.copy2(reg_path, bak)
            with open(reg_path, "r", encoding="utf-8", errors="surrogateescape") as fp:
                raw = fp.read()
            new, changes = patch_reg_text_locale(raw, preset, is_system=is_system)
            new2, extra_chg = register_reg_font_families(new, extra_families or [], is_system=is_system)
            new = new2
            changes.extend(extra_chg)
            if new != raw:
                with open(reg_path, "w", encoding="utf-8", errors="surrogateescape") as fp:
                    fp.write(new)
                result["changes"].extend(
                    [f"{'system' if is_system else 'user'}:{c}" for c in changes]
                )
            else:
                result["changes"].append(f"{'system' if is_system else 'user'}:unchanged")
        except Exception as e:  # noqa: BLE001
            logger.error("patch reg %s: %s", reg_path, e)
            result["errors"].append(f"{os.path.basename(reg_path)}: {e}")

    result["ok"] = bool(result["changes"]) and not any(
        "error" in str(e).lower() for e in result["errors"]
    )
    if result["changes"] and not result["errors"]:
        result["ok"] = True
    elif result["changes"]:
        result["ok"] = True
    return result


def update_shortcut_launch_options(
    *,
    appid: Any,
    userdata_id: str = "",
    key: str = "",
    unix_lang: str = "zh_CN.UTF-8",
    overwrite_lang: bool = True,
    find_steam_root,
    normalize_appid,
    read_node,
    write_vdf,
) -> Dict[str, Any]:
    """为匹配的非 Steam 快捷方式写入 LANG 启动项。"""
    root = find_steam_root()
    if not root:
        return {"updated": 0, "details": [], "message": "无 Steam 目录"}

    target_appid = normalize_appid(appid)
    target_key = str(key).strip() if key is not None and str(key).strip() != "" else ""
    prefer_sid = str(userdata_id or "").strip()
    ud_root = os.path.join(root, "userdata")
    if not os.path.isdir(ud_root):
        return {"updated": 0, "details": [], "message": "无 userdata"}

    sids: List[str] = []
    if prefer_sid and os.path.isdir(os.path.join(ud_root, prefer_sid)):
        sids.append(prefer_sid)
    for sid in sorted(os.listdir(ud_root)):
        if sid not in sids:
            sids.append(sid)

    updated = 0
    details: List[Dict[str, Any]] = []

    for sid in sids:
        sc_path = os.path.join(ud_root, sid, "config", "shortcuts.vdf")
        if not os.path.isfile(sc_path):
            continue
        try:
            with open(sc_path, "rb") as fp:
                parsed = read_node(fp)
        except Exception as e:  # noqa: BLE001
            details.append({"userdata_id": sid, "error": str(e)})
            continue

        shortcuts = parsed.get("shortcuts")
        if not isinstance(shortcuts, dict):
            continue

        dirty = False
        for k, entry in list(shortcuts.items()):
            if not isinstance(entry, dict):
                continue
            hit = False
            if target_key and str(k) == target_key and (not prefer_sid or sid == prefer_sid):
                hit = True
            if not hit and target_appid:
                try:
                    if normalize_appid(entry.get("appid")) == target_appid:
                        hit = True
                except Exception:  # noqa: BLE001
                    pass
            if not hit:
                continue

            old_lo = str(entry.get("LaunchOptions") or "")
            m_old = re.search(r"(?i)\bLANG=([^\s]+)", old_lo)
            old_lang = (m_old.group(1) if m_old else "").strip()
            # 批量修时不覆盖已经是另一种中日文的启动项
            if (
                not overwrite_lang
                and old_lang
                and old_lang != unix_lang
                and any(x in old_lang for x in ("zh_", "ja_"))
            ):
                details.append(
                    {
                        "userdata_id": sid,
                        "key": str(k),
                        "name": entry.get("AppName"),
                        "appid": normalize_appid(entry.get("appid")),
                        "launch_options": old_lo,
                        "skipped": "other_lang",
                    }
                )
                continue
            new_lo = build_cjk_launch_options(old_lo, unix_lang)
            if old_lo.strip() != new_lo.strip():
                entry["LaunchOptions"] = new_lo
                dirty = True
                updated += 1
                details.append(
                    {
                        "userdata_id": sid,
                        "key": str(k),
                        "name": entry.get("AppName"),
                        "appid": normalize_appid(entry.get("appid")),
                        "launch_options": new_lo,
                    }
                )
            else:
                details.append(
                    {
                        "userdata_id": sid,
                        "key": str(k),
                        "name": entry.get("AppName"),
                        "appid": normalize_appid(entry.get("appid")),
                        "launch_options": new_lo,
                        "unchanged": True,
                    }
                )

        if dirty:
            try:
                bak = sc_path + f".bak_nsc_cjk_{int(time.time())}"
                shutil.copy2(sc_path, bak)
                write_vdf(sc_path, parsed)
            except Exception as e:  # noqa: BLE001
                logger.error("write launch options failed %s: %s", sc_path, e)
                details.append({"userdata_id": sid, "error": f"write: {e}"})

    found = len(details)
    if updated:
        msg = f"已更新 {updated} 条启动项"
    elif found:
        msg = f"启动项已是目标值（{found} 条）"
    else:
        msg = "未找到对应快捷方式"
    return {
        "updated": updated,
        "details": details,
        "message": msg,
    }


def repair_cjk_fonts_for_game(
    *,
    appid: Any = 0,
    userdata_id: str = "",
    key: str = "",
    name: str = "",
    lang: str = "zh_CN",
    overwrite_lang: bool = True,
    start_dir: str = "",
    exe: str = "",
    font_size: Any = 0,
    collect_prefix_dirs,
    find_steam_root,
    normalize_appid,
    read_node,
    write_vdf,
) -> Dict[str, Any]:
    """修复单个非 Steam 游戏的汉化字体/区域设置。"""
    preset = resolve_cjk_preset(lang)
    aid = normalize_appid(appid)
    out: Dict[str, Any] = {
        "success": False,
        "appid": aid,
        "name": name or "",
        "lang": preset["key"],
        "lang_label": preset["label"],
        "prefix_results": [],
        "launch": {},
        "font_size": {},
        "message": "",
    }
    if not aid:
        out["message"] = "无效 appid"
        return out

    game_fonts = install_game_local_cjk_fonts(start_dir=start_dir, exe=exe, preset=preset)
    out["game_fonts"] = game_fonts
    # RGSS/HTML Maker 检测到的具体字体名 + 与引擎无关的常见系统字体别名兜底
    # （吉里吉里/NScripter/Artemis 等非 RPG Maker 的老 galgame 也能覆盖到）。
    extra_fams: List[Dict[str, str]] = list(game_fonts.get("prefix_families") or [])
    seen_fams = {str(f.get("family")) for f in extra_fams if isinstance(f, dict)}
    for item in default_common_font_aliases(preset):
        if item["family"] not in seen_fams:
            extra_fams.append(item)
            seen_fams.add(item["family"])
    font_size_r = patch_game_font_size(start_dir=start_dir, exe=exe, font_size=font_size)
    out["font_size"] = font_size_r

    prefixes = collect_prefix_dirs(aid, "compatdata")
    if prefixes:
        for p in prefixes:
            out["prefix_results"].append(
                patch_proton_prefix_cjk(p, preset, extra_families=extra_fams)
            )
    else:
        out["prefix_results"].append(
            {
                "prefix": "",
                "ok": False,
                "changes": [],
                "errors": ["尚未生成 compatdata（启动一次游戏后再修前缀更完整）"],
            }
        )

    out["launch"] = update_shortcut_launch_options(
        appid=aid,
        userdata_id=userdata_id,
        key=key,
        unix_lang=preset["unix_lang"],
        overwrite_lang=overwrite_lang,
        find_steam_root=find_steam_root,
        normalize_appid=normalize_appid,
        read_node=read_node,
        write_vdf=write_vdf,
    )

    prefix_changed = sum(len(r.get("changes") or []) for r in out["prefix_results"])
    game_font_changed = bool(game_fonts.get("changes"))
    launch_updated = int(out["launch"].get("updated") or 0) > 0
    font_size_changed = bool(font_size_r.get("changes")) and not font_size_r.get("skipped")
    any_changed = prefix_changed > 0 or game_font_changed or launch_updated or font_size_changed

    out["success"] = True
    parts = [f"[{preset['label']}] {name or aid}"]
    if prefixes:
        parts.append(f"前缀修补 {len(prefixes)} 个({prefix_changed} 项变更)")
    else:
        parts.append("无前缀(仅写启动项，建议先启动一次游戏生成 Proton 前缀后再修一次)")
    if game_fonts.get("changes"):
        parts.append(f"游戏Fonts补了 {len(game_fonts['changes'])} 项")
    elif game_fonts.get("errors"):
        parts.append("游戏Fonts:" + ",".join(game_fonts["errors"][:2]))
    if font_size_r.get("skipped"):
        pass
    elif font_size_r.get("changes"):
        parts.append("字号 " + ",".join(str(x) for x in font_size_r["changes"][:3]))
    elif font_size_r.get("errors"):
        parts.append("字号:" + ",".join(font_size_r["errors"][:2]))
    parts.append(out["launch"].get("message") or "")
    # 前缀存在但这次实际什么都没改（多半是已经修过、处于目标状态），
    # 明确说出来，不要让人以为是刚生效的一次新修复。
    if not any_changed and prefixes:
        parts.insert(1, "本次未产生新变更(可能已是目标状态)")
    parts.append("请完全退出 Steam 再启动游戏验证。")
    out["message"] = "；".join(p for p in parts if p)
    out["changed"] = any_changed
    out["prefix_ok"] = any(bool(r.get("changes")) for r in out["prefix_results"])
    out["launch_ok"] = int(out["launch"].get("updated") or 0) > 0 or any(
        d.get("unchanged") for d in (out["launch"].get("details") or [])
    )
    return out


def repair_cjk_fonts_batch(
    *,
    appids: Optional[List[Any]] = None,
    lang: str = "zh_CN",
    only_with_prefix: bool = False,
    font_size: Any = 0,
    overwrite_lang: bool = False,
    games: Optional[List[Dict[str, Any]]] = None,
    collect_prefix_dirs,
    find_steam_root,
    normalize_appid,
    read_node,
    write_vdf,
) -> Dict[str, Any]:
    """批量修复。appids 为空则处理传入的 games 列表。

    overwrite_lang 默认 False：已经设过**别的**语言的游戏会被保留，避免批量操作
    覆盖用户逐个调好的设置。但选错语言后想全部改过来时必须传 True —— 否则这些
    游戏会被静默跳过，而且仍然计入「已处理」，看起来成功了其实一条都没改。
    """
    preset = resolve_cjk_preset(lang)
    if games is None:
        games = []

    want: Optional[set] = None
    if appids:
        want = {normalize_appid(a) for a in appids if normalize_appid(a)}

    fixed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for g in games:
        aid = normalize_appid(g.get("appid"))
        if not aid:
            continue
        if want is not None and aid not in want:
            continue
        if only_with_prefix and not collect_prefix_dirs(aid, "compatdata"):
            skipped.append({"appid": aid, "name": g.get("name"), "reason": "no_prefix"})
            continue
        try:
            r = repair_cjk_fonts_for_game(
                appid=aid,
                userdata_id=str(g.get("userdata_id") or ""),
                key=str(g.get("key") if g.get("key") is not None else ""),
                name=str(g.get("name") or ""),
                lang=preset["key"],
                overwrite_lang=overwrite_lang,
                start_dir=str(g.get("start_dir") or ""),
                exe=str(g.get("exe") or ""),
                font_size=font_size,
                collect_prefix_dirs=collect_prefix_dirs,
                find_steam_root=find_steam_root,
                normalize_appid=normalize_appid,
                read_node=read_node,
                write_vdf=write_vdf,
            )
            fixed.append(r)
        except Exception as e:  # noqa: BLE001
            logger.error("repair cjk failed appid=%s: %s", aid, e)
            skipped.append({"appid": aid, "name": g.get("name"), "reason": str(e)})

    ok_n = sum(1 for f in fixed if f.get("success"))
    # 启动项因为「已经是另一种语言」而没改的，必须单独报出来。
    # 它们的 success 仍是 True（前缀该修的都修了），只统计成功数会让人以为
    # 语言已经换过去了，实际上一条启动项都没动。
    kept_lang: List[Dict[str, Any]] = []
    for f in fixed:
        for d in (f.get("launch") or {}).get("details") or []:
            if d.get("skipped") == "other_lang":
                kept_lang.append(
                    {
                        "appid": d.get("appid"),
                        "name": d.get("name"),
                        "launch_options": d.get("launch_options"),
                    }
                )

    return {
        "success": True,
        "lang": preset["key"],
        "lang_label": preset["label"],
        "fixed_count": ok_n,
        "skipped_count": len(skipped),
        "kept_other_lang_count": len(kept_lang),
        "kept_other_lang": kept_lang[:40],
        "fixed": fixed[:80],
        "skipped": skipped[:40],
        "message": (
            f"已按「{preset['label']}」处理 {ok_n} 个非 Steam 游戏"
            + (f"，跳过 {len(skipped)} 个" if skipped else "")
            + (
                f"；其中 {len(kept_lang)} 个已设过别的语言，启动项保持原样未改"
                "（要改成当前语言请用「覆盖已设语言」）"
                if kept_lang
                else ""
            )
            + "。请完全退出 Steam 再启动对应游戏。"
        ),
    }


# ===========================================================================
# 自动检测该用哪个 CJK 预设
# ---------------------------------------------------------------------------
# 老的日文 / 汉化 Windows 游戏都是 ANSI 程序：它读自己的路径、配置文件、
# 数据文件名时，都要过一遍系统 ANSI 代码页。代码页选错的后果分两级：
#   轻 —— 文字显示成 ?? 或乱码；
#   重 —— 找不到自己的数据文件、建不出存档目录，启动即弹框退出。
# 后一种最难查，因为游戏什么日志都不留，界面上只有一个没头没尾的错误框。
#
# 判据按可信度排序：
#   1. 硬证据：引擎配置里写的数据文件名，用哪个代码页解码才对得上磁盘上真实
#      存在的文件。对不上游戏就读不到自己的数据，这是唯一解，一票定音。
#   2. 数据文件名的性质：是「真文本」还是「某代码页的乱码」。
#   3. 配置里的 GameName —— 它决定存档目录名。
#   4. 汉化组的 CN / CHS 人为标记。
#   5. 随包说明文档、字体、文件名整体倾向（弱证据，兜底用）。
# ===========================================================================

CJK_CONFIG_FILES = ("AliceStart.ini", "alicestart.ini", "System40.ini", "System39.ini")
CJK_DATA_EXTS = (".ain", ".ald", ".afa", ".alk", ".wai", ".bgi")

# 常用字表：用来区分「真中文」和「字都合法但根本没人这么写」的误读产物。
# 例如 GBK 的「兰斯9」被 Big5 读成「擘佴9」，两个字都是合法汉字，
# 但没有一个常用字，命中率立刻把这种误读打下去。
_CJK_COMMON_SC = frozenset(
    "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要"
    "出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没"
    "成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又"
    "行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高"
    "已亲其进此话常与活正感汉化组版中文简体游戏兰斯完全修改编辑刻碟贩售"
)
_CJK_COMMON_TC = frozenset(
    "的一是了我不人在他有這個上們來到時大地為子中你說生國年著就那和要"
    "出也得裡後自以會家可下而過天去能對小多然於心學麼之都好看起發當沒"
    "成只如事把還用第樣道想作種開美總從無情己面最女但現前些所同日手又"
    "行意動方期它頭經長兒回位分愛老因很給名法間斯知世什兩次使身者被高"
    "已親其進此話常與活正感漢化組版中文簡體遊戲蘭斯完全修改編輯刻碟販售"
)


def cjk_codepage_for_lang(lang: str) -> str:
    """预设 -> Python 编解码器名。"""
    preset = resolve_cjk_preset(lang)
    return "cp" + str(preset.get("acp") or "936")


def cjk_text_quality(s: str) -> float:
    """0~1，越高越像人写的正常文本。

    乱码最稳定的特征是半角片假名（UTF/GBK 文本被当成 Shift-JIS 读的产物）
    和私用区字符，所以这两类直接重罚。
    """
    if not s:
        return 0.0
    good = bad = 0
    for c in s:
        o = ord(c)
        if 0xFF61 <= o <= 0xFF9F:
            bad += 2
        elif 0xE000 <= o <= 0xF8FF:
            bad += 3
        elif c == "�":
            bad += 3
        elif o < 0x80:
            good += 1
        elif 0x3040 <= o <= 0x30FF:
            good += 2
        elif 0x4E00 <= o <= 0x9FFF:
            good += 1
        elif 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFF60:
            good += 1
        else:
            bad += 1
    total = good + bad
    return good / total if total else 0.0


def _cjk_script_hint(s: str) -> tuple:
    """返回 (常用简体命中数, 常用繁体命中数, 假名数)。"""
    sc = sum(1 for c in s if c in _CJK_COMMON_SC)
    tc = sum(1 for c in s if c in _CJK_COMMON_TC)
    kana = sum(1 for c in s if 0x3040 <= ord(c) <= 0x30FF)
    return sc, tc, kana


def cjk_score_text_for_lang(s: str, lang: str) -> float:
    """一段文本按某语言解出来之后有多可信。质量分 + 常用字命中率。"""
    q = cjk_text_quality(s)
    sc, tc, kana = _cjk_script_hint(s)
    n = max(len(s), 1)
    if lang == "zh_CN":
        return q + 2.0 * sc / n
    if lang == "zh_TW":
        return q + 2.0 * tc / n
    return q + 2.0 * kana / n


def _cjk_decode(data: bytes, cp: str) -> Optional[str]:
    try:
        return data.decode(cp)
    except Exception:  # noqa: BLE001
        return None


def _cjk_read_config(game_dir: str) -> tuple:
    for name in CJK_CONFIG_FILES:
        p = os.path.join(game_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as fp:
                return name, fp.read()
        except OSError:
            continue
    return None, None


def _cjk_ini_value(raw: bytes, key: bytes) -> Optional[bytes]:
    # 行尾用 (?=[\r\n]|\Z) 而不是吃掉一个换行符，否则文件最后一行没有换行时读不到。
    for k, v in re.findall(rb"^[ \t]*(\w+)[ \t]*=[ \t]*(.*?)(?=[\r\n]|\Z)", raw, re.M):
        if k.lower() == key:
            return v.strip().strip(b'"')
    return None


def classify_cjk_filename(name: str) -> tuple:
    """一个非 ASCII 文件名是「真文本」还是「乱码」。

    儔儞僗俇偦偺屻.ain  -> Shift-JIS 字节被 GBK 解读出来的，游戏必须跑在 GBK 下
    三匹が.ain          -> 本身就是正常日文名，游戏必须跑在 Shift-JIS 下
    """
    q_asis = cjk_text_quality(name)
    try:
        reinterp = name.encode("cp936").decode("cp932")
    except Exception:  # noqa: BLE001
        reinterp = None
    q_re = cjk_text_quality(reinterp) if reinterp else 0.0
    if reinterp and q_re > q_asis + 0.25:
        return "needs_gbk", "%s 按 GBK 还原成「%s」才是正常日文" % (name, reinterp)
    if q_asis > 0.7 and any(0x3040 <= ord(c) <= 0x30FF for c in name):
        return "native_jp", "%s 本身就是正常日文名" % name
    return "neutral", ""


def check_path_codepage(path: str, lang: str) -> Dict[str, Any]:
    """游戏路径能否被该预设的 ANSI 代码页表示。

    表示不了就是死局：游戏连自己的目录都找不到，装什么字体都没用，
    只能把路径改成该代码页能表示的字符（最保险是纯 ASCII）。
    """
    cp = cjk_codepage_for_lang(lang)
    bad = []
    for c in set(path or ""):
        if c in "/\\ ":
            continue
        try:
            c.encode(cp)
        except Exception:  # noqa: BLE001
            bad.append(c)
    bad.sort()
    return {
        "ok": not bad,
        "path": path,
        "codepage": cp,
        "bad_chars": "".join(bad),
        "message": (
            ""
            if not bad
            else "路径含「%s」，%s 代码页无法表示。游戏会找不到自己的目录，"
            "请先把路径改成英文（纯 ASCII 最保险）再修复。" % ("".join(bad), cp)
        ),
    }


def _cjk_hint_cn_marker(game_dir, names, scores, reasons) -> None:
    """汉化组习惯把改过的可执行文件/数据标上 CN / CHS，这是很硬的人为标记。"""
    for n in names:
        stem, ext = os.path.splitext(n)
        if ext.lower() not in (".exe", ".ain"):
            continue
        up = stem.upper()
        if up.endswith("CN") or "CHS" in up or up.endswith("_SC"):
            scores["zh_CN"] += 20
            reasons.append("[汉化标记] %s 带 CN/CHS 标记，是汉化组改过的版本 -> GBK" % n)
            return


def _cjk_hint_aux(game_dir, names, scores, reasons) -> None:
    """随包字体和说明文档。

    说明书是弱证据 —— 汉化版经常原封不动保留日文原版的 readme，
    权重必须压在游戏名之下，否则会把汉化版误判成日文版。
    """
    for n in names:
        low = n.lower()
        if low.endswith((".ttf", ".otf", ".ttc")) and any(
            k in low for k in ("simhei", "cn", "sc", "hei", "song", "kai", "yahei")
        ):
            scores["zh_CN"] += 15
            reasons.append("[随包字体] %s 是中文字体，说明是简中汉化版 -> GBK" % n)
            break
    best = None
    for n in names:
        if not n.lower().endswith((".txt", ".md")):
            continue
        try:
            with open(os.path.join(game_dir, n), "rb") as fp:
                raw = fp.read(4000)
        except OSError:
            continue
        if raw.isascii():
            continue
        for lang in CJK_LANG_PRESETS:
            t = _cjk_decode(raw, cjk_codepage_for_lang(lang))
            if t is None:
                continue
            s = cjk_score_text_for_lang(t, lang)
            if best is None or s > best[0]:
                best = (s, lang, n)
    if best and best[0] > 0.9:
        scores[best[1]] += 6
        reasons.append(
            "[说明文档] %s 按%s解码最通顺（弱证据）"
            % (best[2], CJK_LANG_PRESETS[best[1]]["label"])
        )


def _cjk_hint_filenames(names, scores, reasons) -> None:
    """目录里非 ASCII 文件名整体偏简中还是偏日文。兜底用。"""
    blob = "".join(n for n in names if not n.isascii())
    if not blob:
        return
    sc, _tc, kana = _cjk_script_hint(blob)
    if sc >= 3 and sc > kana:
        scores["zh_CN"] += 8
        reasons.append("[文件名] 目录内文件名多为简体中文（命中常用字 %d 处）" % sc)
    elif kana >= 3 and kana > sc:
        scores["ja_JP"] += 8
        reasons.append("[文件名] 目录内文件名多为日文假名（%d 处）" % kana)


def detect_cjk_lang_for_dir(game_dir: str, exe: str = "") -> Dict[str, Any]:
    """检测某个游戏目录该用哪个 CJK 预设。

    exe 是实际会被启动的可执行文件。它很重要：同一个目录里常常既有原版
    System40.exe 又有汉化版 System40CN.exe，而配置文件 System40.ini 里的
    CodeName 记的往往是**原版**的数据文件名。这时候如果只看 ini，会把汉化版
    误判成日文版 —— 所以带 CN/CHS 标记的启动 exe 的权重要压过 ini 的硬证据。
    """
    result: Dict[str, Any] = {
        "ok": False,
        "dir": game_dir,
        "exe": exe,
        "lang": "zh_CN",
        "confidence": "低",
        "scores": {},
        "reasons": [],
        "config": "",
        "hard_evidence": False,
    }
    if not game_dir or not os.path.isdir(game_dir):
        result["reasons"].append("目录不存在，无法检测")
        return result

    langs = list(CJK_LANG_PRESETS)
    scores = {l: 0 for l in langs}
    reasons: List[str] = []
    try:
        names = os.listdir(game_dir)
    except OSError as exc:
        result["reasons"].append("读取目录失败：%s" % exc)
        return result

    cfg_name, raw = _cjk_read_config(game_dir)
    hard = False

    # 判据 0：实际启动的 exe 自带汉化标记 —— 比 ini 更可信，因为 ini 描述的
    # 常常是同目录下那个没被汉化的原版 exe。
    exe_stem = os.path.splitext(os.path.basename((exe or "").strip('"')))[0].upper()
    exe_is_cn = bool(exe_stem) and (
        exe_stem.endswith("CN") or "CHS" in exe_stem or exe_stem.endswith("_SC")
    )
    if exe_is_cn:
        scores["zh_CN"] += 120
        hard = True
        reasons.append(
            "[启动项] 实际启动的是 %s，带 CN/CHS 汉化标记 -> GBK"
            "（同目录 ini 里记的多半是原版日文 exe 的信息，不作数）"
            % os.path.basename(exe)
        )

    # 判据 1：数据文件名必须对得上磁盘（硬证据）
    # exe 已经自报汉化身份时跳过：此时 ini 描述的是同目录那个原版 exe，
    # 让它参与只会把结论拉回日文。
    if raw and not exe_is_cn:
        code = _cjk_ini_value(raw, b"codename")
        if code and not code.isascii():
            for lang in langs:
                name = _cjk_decode(code, cjk_codepage_for_lang(lang))
                if name and os.path.isfile(os.path.join(game_dir, name)):
                    scores[lang] += 100
                    hard = True
                    reasons.append(
                        "[硬证据] %s 里的数据文件名按%s解码 = %s，磁盘上确实有这个文件"
                        % (cfg_name, CJK_LANG_PRESETS[lang]["label"], name)
                    )
            if not hard:
                reasons.append("[警告] %s 里的数据文件名在任何代码页下都对不上磁盘文件" % cfg_name)
        elif code:
            reasons.append("%s 的数据文件名 %s 是 ASCII，不构成约束" % (cfg_name, code.decode()))

    # 判据 2：数据文件名的性质（有硬证据时不再参与，避免自相矛盾）
    if not hard:
        seen: Dict[str, str] = {}
        for n in names:
            if n.isascii() or not n.lower().endswith(CJK_DATA_EXTS):
                continue
            verdict, why = classify_cjk_filename(n)
            if verdict != "neutral" and verdict not in seen:
                seen[verdict] = why
        if "needs_gbk" in seen:
            scores["zh_CN"] += 40
            reasons.append("[数据文件] %s -> 必须 GBK" % seen["needs_gbk"])
        if "native_jp" in seen:
            scores["ja_JP"] += 40
            reasons.append("[数据文件] %s -> 必须 Shift-JIS" % seen["native_jp"])

    # 判据 3：GameName 决定存档目录名
    if raw:
        title = _cjk_ini_value(raw, b"gamename")
        if title and not title.isascii():
            cand = []
            for lang in langs:
                t = _cjk_decode(title, cjk_codepage_for_lang(lang))
                if t is not None:
                    cand.append((cjk_score_text_for_lang(t, lang), lang, t))
            cand.sort(key=lambda x: x[0], reverse=True)
            if cand:
                s, lang, t = cand[0]
                scores[lang] += 10
                reasons.append(
                    "[游戏名] 按%s解码最通顺(%.2f) = %s"
                    % (CJK_LANG_PRESETS[lang]["label"], s, t[:30])
                )

    # 判据 4/5：硬证据缺席时的兜底
    if not hard:
        _cjk_hint_cn_marker(game_dir, names, scores, reasons)
        _cjk_hint_aux(game_dir, names, scores, reasons)
        _cjk_hint_filenames(names, scores, reasons)

    best = max(scores, key=lambda l: scores[l])
    top = scores[best]
    second = max([v for k, v in scores.items() if k != best] or [0])
    if top == 0:
        confidence = "低"
    elif hard or top - second >= 25:
        confidence = "高"
    elif top - second >= 10:
        confidence = "中"
    else:
        confidence = "低"

    lang = best if top else "zh_CN"
    result.update(
        {
            "ok": True,
            "lang": lang,
            "lang_label": CJK_LANG_PRESETS[lang]["label"],
            "confidence": confidence,
            "scores": scores,
            "reasons": reasons,
            "config": cfg_name or "",
            "hard_evidence": hard,
            "path_check": check_path_codepage(game_dir, lang),
        }
    )
    if top == 0:
        result["reasons"].append("没找到任何有效线索，回退到默认的简体中文；建议两个预设都试一次")
    return result
