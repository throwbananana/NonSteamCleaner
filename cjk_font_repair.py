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

        if preset.get("need_heiti") == "1" and '"SimHei (TrueType)"="simhei.ttf"' not in s:
            if '"Microsoft YaHei (TrueType)"="msyh.ttf"' in s:
                s = s.replace(
                    '"Microsoft YaHei (TrueType)"="msyh.ttf"',
                    '"Microsoft YaHei (TrueType)"="msyh.ttf"\n'
                    '"SimHei (TrueType)"="simhei.ttf"\n'
                    '"黑体 (TrueType)"="simhei.ttf"',
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


def ensure_simhei_font_links(fonts_dir: str) -> List[str]:
    """在 Windows Fonts 目录创建 simhei 指向可用中文字体。"""
    done: List[str] = []
    if not os.path.isdir(fonts_dir):
        return done
    candidates = [
        os.path.join(fonts_dir, "msyh.ttf"),
        os.path.join(fonts_dir, "simsun.ttc"),
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/run/host/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    target = None
    for c in candidates:
        if os.path.lexists(c):
            target = c
            break
    if not target:
        return done

    fonts_real = os.path.realpath(fonts_dir)
    for name in ("simhei.ttf", "simhei.ttc"):
        dest = os.path.join(fonts_dir, name)
        try:
            if os.path.dirname(os.path.realpath(target)) == fonts_real or os.path.dirname(
                target
            ) == fonts_dir:
                link_to = os.path.basename(target)
            else:
                link_to = target
            if os.path.islink(dest) or os.path.isfile(dest):
                # 已存在
                done.append(f"{name}=exists")
                continue
            if os.path.lexists(dest):
                os.unlink(dest)
            os.symlink(link_to, dest)
            done.append(f"{name}->{link_to}")
        except Exception as e:  # noqa: BLE001
            logger.warning("simhei link failed %s: %s", dest, e)
            done.append(f"{name}:error:{e}")
    return done


def patch_proton_prefix_cjk(prefix_dir: str, preset: Dict[str, str]) -> Dict[str, Any]:
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

    if preset.get("need_heiti") == "1":
        links = ensure_simhei_font_links(fonts_dir)
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
        "message": "",
    }
    if not aid:
        out["message"] = "无效 appid"
        return out

    prefixes = collect_prefix_dirs(aid, "compatdata")
    if prefixes:
        for p in prefixes:
            out["prefix_results"].append(patch_proton_prefix_cjk(p, preset))
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
        find_steam_root=find_steam_root,
        normalize_appid=normalize_appid,
        read_node=read_node,
        write_vdf=write_vdf,
    )

    out["success"] = True
    parts = [f"[{preset['label']}] {name or aid}"]
    if prefixes:
        chg_n = sum(len(r.get("changes") or []) for r in out["prefix_results"])
        parts.append(f"前缀修补 {len(prefixes)} 个({chg_n} 项变更)")
    else:
        parts.append("无前缀(仅写启动项)")
    parts.append(out["launch"].get("message") or "")
    parts.append("请完全退出 Steam 再启动游戏验证。")
    out["message"] = "；".join(p for p in parts if p)
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
    games: Optional[List[Dict[str, Any]]] = None,
    collect_prefix_dirs,
    find_steam_root,
    normalize_appid,
    read_node,
    write_vdf,
) -> Dict[str, Any]:
    """批量修复。appids 为空则处理传入的 games 列表。"""
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
    return {
        "success": True,
        "lang": preset["key"],
        "lang_label": preset["label"],
        "fixed_count": ok_n,
        "skipped_count": len(skipped),
        "fixed": fixed[:80],
        "skipped": skipped[:40],
        "message": (
            f"已按「{preset['label']}」处理 {ok_n} 个非 Steam 游戏"
            + (f"，跳过 {len(skipped)} 个" if skipped else "")
            + "。请完全退出 Steam 再启动对应游戏。"
        ),
    }
