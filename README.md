# 非 Steam 游戏清理 (NonSteamCleaner)

Steam Deck 的 **Decky Loader** 插件，用来管理库里的非 Steam 游戏。

版本 **1.4.9**。 [![Release](https://img.shields.io/github/v/release/throwbananana/NonSteamCleaner)](https://github.com/throwbananana/NonSteamCleaner/releases/latest)

## 功能

### 清理
四个范围，二次确认后删除：

1. **删除本体** —— 可执行文件 + `StartDir`（过浅或与其它快捷方式共用时只删 exe）
2. **删除本体 + 存档** —— 另删 `compatdata/<appid>`
3. **删除本体 + 存档 + 着色器缓存**
4. **删除本体 + 着色器缓存** —— 保留前缀/存档

同时移除 `shortcuts.vdf` 条目和对应网格图。Steam 运行中会提示：改完后必须完全退出再打开。

入口：游戏详情页、右键菜单、插件面板「已入库非 Steam」。

### 回收站（默认开启）

删除时文件不会被直接抹掉，而是移进**所在分区**的回收站，删错了可以还原：

- 回收站按分区分布：home 分区在 `~/.local/share/NonSteamCleaner/trash`，SD 卡等其它分区在挂载点下的 `.nonsteamcleaner-trash`。同分区内 `mv`，几十 GB 的游戏也是瞬间完成，不额外占空间。
- 目录名以点开头，扫描添加不会把回收站里的 exe 重新扫出来。
- 超过 14 天的条目在插件启动时自动清理；也可手动「彻底删除」单项或清空。
- 还原只恢复文件，**Steam 库里的快捷方式不会自动加回来**，需要用「扫描添加」重新加入。
- 开关在「清理 → 回收站」。关掉后删除即为直接抹除，不可恢复。

### 磁盘占用 / 孤儿数据

- **统计磁盘占用**：按「本体 + 存档/前缀 + 着色器缓存」算出每个非 Steam 游戏占了多少空间，从大到小排。共用目录的游戏会标注，总计里只算一次。
- **孤儿数据**：手动删过游戏、或改过名字/路径之后留下的 `compatdata` 前缀、着色器缓存和封面图，库里已经没有快捷方式认领它们。可按类别一键清理（走回收站）。

只认非 Steam 的 appid（`>= 0x80000000`），正牌 Steam 游戏的数据不会被碰。读不到 `shortcuts.vdf` 或有文件解析失败时会拒绝分析——已知清单不完整的情况下判孤儿等于乱删。

### 扫描添加
扫描指定目录下的启动器，勾选后写入 Steam 库。可调扫描深度、自动解压嵌套层数。安装器/补丁/注入器等会尽量过滤。

**解压后删除原压缩包**（默认关，勾选开启）：只删本次真正解压成功的包，解压失败或因目标已存在而跳过的一律不动。分卷压缩包（`xxx.7z.001`、`xxx.part1.rar`）会整套删，不会只删第一卷留下一堆孤儿卷。删除走回收站，可还原；回收站关掉时会直接删，界面上有红字提示。

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

- 回收站开启时删除可还原；关掉回收站后删除不可恢复。确认框会列出真实存在的路径。
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
