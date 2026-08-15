# 非 Steam 游戏清理 (NonSteamCleaner)

Steam Deck 的 **Decky Loader** 插件，用来管理库里的非 Steam 游戏。

版本 **1.4.6**。 [![Release](https://img.shields.io/github/v/release/throwbananana/NonSteamCleaner)](https://github.com/throwbananana/NonSteamCleaner/releases/latest)

## 功能

### 清理
四个范围，二次确认后删除：

1. **删除本体** —— 可执行文件 + `StartDir`（过浅或与其它快捷方式共用时只删 exe）
2. **删除本体 + 存档** —— 另删 `compatdata/<appid>`
3. **删除本体 + 存档 + 着色器缓存**
4. **删除本体 + 着色器缓存** —— 保留前缀/存档

同时移除 `shortcuts.vdf` 条目和对应网格图。Steam 运行中会提示：改完后必须完全退出再打开。

入口：游戏详情页、右键菜单、插件面板「已入库非 Steam」。

### 扫描添加
扫描指定目录下的启动器，勾选后写入 Steam 库。可调扫描深度、自动解压嵌套层数。安装器/补丁/注入器等会尽量过滤。

### 失效 / 重复快捷方式
- 启动文件已不在磁盘：只从库里移除快捷方式
- 同一 exe 被加多次：可只留一条（不删文件）

### 截图设为图标
把当前画面或 Steam 截图写成库图标/封面。游戏模式若直接截屏失败，请先按 Steam+R1（或 F12），再点「用最新截图」。

### 修复汉化字体
老汉化 / 日文游戏文字变成 **`??`** 时：

- 写入启动项 `LANG` / `LC_ALL`（简中 / 日文 / 繁中）
- 修补 Proton 前缀代码页与区域
- 中文补丁额外映射黑体 / SimHei

详情页和右键可选语言；插件面板可批量修。

### 其它
- 隐藏栏：扫描误报可藏起来
- `-trouble`：给游戏文件夹加标记，不删除
- 为已添加游戏补写库图标；恢复被 logo 挡住的详情页标题

---

## 安装

### 方式一：下载打包好的 Release（推荐）

前往 [Releases 页面](https://github.com/throwbananana/NonSteamCleaner/releases/latest) 下载最新的 `NonSteamCleaner.tar.gz`，解压后得到的 `NonSteamCleaner/` 文件夹整体放入 `/home/deck/homebrew/plugins/`，重启 Decky / Steam 即可。

### 方式二：从源码构建

```bash
cd /home/deck/nonsteam-cleaner
npm install
npm run build
sudo cp -a /home/deck/nonsteam-cleaner/main.py \
  /home/deck/nonsteam-cleaner/cjk_font_repair.py \
  /home/deck/nonsteam-cleaner/plugin.json \
  /home/deck/nonsteam-cleaner/dist \
  /home/deck/homebrew/plugins/NonSteamCleaner/
```

文件夹名需与 `plugin.json` 的 `name` 一致（`NonSteamCleaner`）。然后重启 Decky / Steam。

---

## 使用注意

- 删除不可恢复。确认框会列出真实存在的路径。
- `compatdata/<appid>` 同时包含 Proton 前缀和存档。
- 受保护目录（`/`、`/home`、`/usr`、`Downloads` 等）不会整目录删除。
- 改 shortcuts / 图标后请**完全退出 Steam** 再打开。
- 删除前请先关闭对应游戏。

---

## 文件结构

```
nonsteam-cleaner/
├── plugin.json
├── main.py              # Python 后端
├── cjk_font_repair.py   # 汉化字体/区域修补
├── src/
│   ├── index.tsx        # 主 UI + 库页/菜单注入
│   └── patch.tsx        # 实验性：管理选项卡（默认未启用）
└── dist/index.js        # 构建产物
```
