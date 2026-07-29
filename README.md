# 非 Steam 游戏清理 (NonSteamCleaner)

一个 Steam Deck 的 **Decky Loader** 插件，用于彻底清理 Steam 中添加的「非 Steam 游戏」。
可在四个范围中选择删除：

1. **删除本体** —— 仅删除游戏本体（可执行文件 + 其所在游戏目录 `StartDir`）
2. **删除本体 + 存档** —— 本体 + `compatdata/<appid>` 前缀（包含 Proton 前缀与存档）
3. **删除本体 + 存档 + 着色器缓存** —— 本体 + 前缀 + `shadercache/<appid>`
4. **删除本体 + 着色器缓存** —— 本体 + 着色器缓存（保留前缀/存档）

同时会清理 Steam 库中的快捷方式（`shortcuts.vdf`）以及对应的网格图片，让游戏从库中彻底消失。

---

## 安装

### 1. 构建前端
```bash
cd /home/deck/nonsteam-cleaner
npm install
npm run build        # 生成 dist/index.js
```

### 2. 放入 Decky 插件目录
```bash
cp -r /home/deck/nonsteam-cleaner /home/deck/homebrew/plugins/NonSteamCleaner
```
> 文件夹名需与 `plugin.json` 的 `name` 一致（这里用 `NonSteamCleaner`）。

### 3. 重启 Decky / Steam
在游戏模式或桌面模式的 Quick Access（... 菜单）→ 齿轮 → Decky，即可看到「非Steam游戏清理」。

---

## 使用

- 打开插件页面，会列出所有已添加的非 Steam 游戏（含 AppID 与可执行文件路径）。
- 点选某个游戏下方的四个选项之一 → 弹出二次确认，列出**真实存在**的待删文件路径 → 确认后执行。
- 删除完成后请**重启 Steam**，游戏才会从库中消失。

---

## 关于「管理」选项卡的注入（实验性）

你希望把清理选项加进 Steam 游戏属性的「管理」选项卡里。由于 Steam 客户端每次更新
都会改变内部组件的导出名和 props 结构，直接硬注入很脆弱。因此：

- **主 UI（插件页面）是完整可用的入口**，已稳定实现四个删除范围。
- `src/patch.tsx` 提供了把入口按钮注入「管理」选项卡的**实验性**代码，默认未启用。
  需要你在 `src/index.tsx` 中 `import { setupManageTabPatch } from './patch'` 并调用它，
  并根据你的 Steam 版本用 React DevTools 校准 `AppProperties` 的导出名与 `appid` 的 props 路径。

这样设计是为了保证：**即使注入失败，插件页面也始终可用，且绝不会误删真实 Steam 游戏的缓存。**

---

## 安全说明

- 所有删除都经过二次确认，并展示确切路径；删除不可恢复。
- `compatdata/<appid>`（「存档」）同时也包含 Proton 前缀（注册表/配置），删除即一并清除。
- 受保护目录（`/`、`/home`、`/usr` 等）不会被删除；路径过浅也会被拒绝。
- 删除前请先关闭对应游戏。

---

## 文件结构

```
nonsteam-cleaner/
├── plugin.json          # 插件元信息
├── main.py              # Python 后端：解析 shortcuts.vdf、计算目标、执行删除
├── package.json
├── rollup.config.js
├── tsconfig.json
├── README.md
└── src/
    ├── index.tsx        # 主 UI（稳定可用）
    └── patch.tsx        # 实验性：注入 Steam「管理」选项卡
```
