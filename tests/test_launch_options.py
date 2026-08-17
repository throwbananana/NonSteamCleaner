#!/usr/bin/env python3
"""build_cjk_launch_options 的回归测试。

    python3 tests/test_launch_options.py

这个函数曾经有个静默 bug：它把 %command% 从原位置抽走、无条件拼到末尾。
Steam 启动项里 %command% 的位置是有语义的 —— 前面是环境变量和包装器，
后面是传给**游戏**的参数。重排之后：

    ~/lsfg %command% -f rom.gba   ->   ~/lsfg -f rom.gba %command%

-f rom.gba 就从「传给游戏」变成了「传给 ~/lsfg」。游戏照常启动，但收不到
任何参数，表现为模拟器突然不加载 ROM，且全程没有任何报错。下面的用例取自
真实被改坏的快捷方式，改动这个函数时请保证它们仍然通过。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cjk_font_repair import build_cjk_launch_options  # noqa: E402

LANG = "zh_CN.UTF-8"
PREFIX = "LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8"

CASES = [
    ("", f"{PREFIX} %command%", "空启动项"),
    ("%command%", f"{PREFIX} %command%", "只有占位符"),
    ("~/lsfg %command%", f"{PREFIX} ~/lsfg %command%", "纯包装器"),
    (
        "~/lsfg %command% -locale zhCN",
        f"{PREFIX} ~/lsfg %command% -locale zhCN",
        "参数在 %command% 之后，必须留在之后",
    ),
    (
        '~/lsfg %command% -f "/games/gba-17.gba"',
        f'{PREFIX} ~/lsfg %command% -f "/games/gba-17.gba"',
        "模拟器 ROM 路径",
    ),
    (
        'DISABLE_VKBASALT=1 ~/lsfg %command% -batch "/x/恶魔城 - 月下.bin"',
        f'{PREFIX} DISABLE_VKBASALT=1 ~/lsfg %command% -batch "/x/恶魔城 - 月下.bin"',
        "含空格的中文路径",
    ),
    (
        "LANG=ja_JP.UTF-8 LC_ALL=ja_JP.UTF-8 ~/lsfg %command% -f rom.gba",
        f"{PREFIX} ~/lsfg %command% -f rom.gba",
        "换语言：旧的 LANG 被替换而不是叠加",
    ),
    (
        "-someopt",
        f"{PREFIX} %command% -someopt",
        "原本没有 %command%：要补一个，否则 LANG= 会被当成游戏的第一个参数",
    ),
]


def main():
    failed = 0
    for src, want, desc in CASES:
        got = build_cjk_launch_options(src, LANG)
        if got == want:
            print("  ok   %s" % desc)
        else:
            failed += 1
            print("  FAIL %s" % desc)
            print("       输入: %r" % src)
            print("       期望: %r" % want)
            print("       实际: %r" % got)
    print("\n%d/%d 通过" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
