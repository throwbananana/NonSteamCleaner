# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**NonSteamCleaner** (非 Steam 游戏清理) is a **Decky Loader** plugin for the Steam Deck. It manages non-Steam games added to the Steam library: deleting them (body/saves/shader cache in any combination), scanning a directory to bulk-add launchers, cleaning up missing/duplicate shortcuts, setting library icons from screenshots, and repairing CJK (Chinese/Japanese) font rendering (`??` garbled text) in old localized/JP Windows games run under Proton.

The codebase has two runtime halves that only talk to each other through Decky's RPC bridge:
- **Frontend** (`src/index.tsx`, TypeScript/React, built to `dist/index.js`) — injects UI into the Steam library detail page, right-click context menu, and the plugin's own panel (`src/patch.tsx` is an experimental, currently-disabled management tab excluded from the TS build).
- **Backend** (`main.py`, plus `cjk_font_repair.py`), a Python 3.10 process managed by Decky Loader on the Deck itself. There is no backend framework beyond Decky's plugin loader — see Architecture below.

This plugin only truly runs on a Steam Deck (or a Linux box with Decky Loader + a real Steam install/`shortcuts.vdf`). There is no local dev server / mock backend, so most backend logic can only be smoke-tested by deploying to a Deck.

## Commands

```bash
npm install       # install frontend deps
npm run build      # rollup -c → dist/index.js (production, minified via terser)
npm run watch       # rollup -c -w
```

There is no lint or test script configured — `package.json` only defines `build`/`watch`. TypeScript is checked as part of the rollup build (`@rollup/plugin-typescript`, `noEmitOnError: false`, so type errors do **not** fail the build — check `tsc --noEmit` manually if you need a hard type-check).

There is no Python test suite. Backend correctness has to be reasoned about by reading `main.py`/`cjk_font_repair.py` directly, since it depends on live Steam/Deck filesystem state.

### Deploying to a Deck

```bash
sudo cp -a main.py cjk_font_repair.py plugin.json dist \
  /home/deck/homebrew/plugins/NonSteamCleaner/
```
The target folder name must match `plugin.json`'s `name` (`NonSteamCleaner`). Then fully restart Decky/Steam — shortcuts.vdf and icon changes are not picked up by a running Steam session.

### Release

Pushing a `v*` tag (or manual dispatch) runs `.github/workflows/release.yml`: it locates the plugin dir (supports the plugin living in a subdirectory, not just repo root), runs `npm ci && npm run build`, strips `node_modules`, tars up the plugin dir (excluding `.git`/`.github`/`node_modules`, top-level folder renamed to the plugin name) into `RUNNER_TEMP` first (avoids tar reading its own output), and attaches it to a GitHub Release with auto-generated notes.

## Architecture

### Frontend ↔ backend RPC

The frontend never imports `decky-frontend-lib` or `react` as bundled modules — rollup.config.js deliberately does **not** resolve them; the source references Decky/Steam's injected globals directly (`SP_REACT`, `DFL`, and `window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit`). The build must emit an ES module whose default export is a callable (`definePlugin`), because Decky Loader v3's ESMODULE_V1 loader does `const plugin_exports = await import(...); let plugin = plugin_exports.default();`.

Every backend RPC method is exposed by adding an `async def` to `class Plugin`, near the bottom of `main.py` — Decky auto-exposes these to `callPluginMethod`. The frontend wraps each one with `api.callable('method_name')` near the top of `src/index.tsx`; when adding a new backend capability, add the method to `Plugin`, then add a matching `callable(...)` binding in the frontend, keeping the name identical on both sides. Responses sometimes come back wrapped (`{ result, success }`); use the existing `unwrapResult()` helper in `src/index.tsx` rather than reaching into raw fields.

### Backend (`main.py`) is organized in layers, top to bottom

1. **Low-level VDF (binary) parsing/writing** — `_read_node`/`_write_node`/`write_vdf`, used to read/patch Steam's `shortcuts.vdf` directly (no external VDF library).
2. **Steam environment discovery** — `find_steam_root`, `iter_steam_library_roots`, `is_steam_running`, appid helpers (`compute_appid`, `normalize_appid`, `is_nonsteam_shortcut_appid` — non-Steam shortcut appids have the high bit set, i.e. `>= 0x80000000`; the frontend has an identical `normalizeAppId`/`isNonSteamShortcutAppId` pair that must stay in sync).
3. **Game enumeration & duplicate/missing detection** — `list_all_nonsteam_games`, `find_duplicate_nonsteam_groups`, `start_dir_shared_with_others` (used to decide whether deleting "body" should delete the whole StartDir or just the exe, when a folder is shared by multiple shortcuts).
4. **Deletion safety** — `_safe_to_delete` / `_normalize` guard against deleting protected paths (`/`, `/home`, `/usr`, `Downloads`, etc. — see README "使用注意"); `_collect_prefix_dirs` locates `compatdata/<appid>` (Proton prefix + saves) and shader cache dirs.
5. **Settings persistence** — `load_settings`/`save_settings` at `_settings_path()`, covering scan depth, hidden scan items, and CJK-repair state.
6. **Scan-and-add pipeline** — `scan_folder_for_games` walks a directory tree scoring candidate executables (`_score_exe`, `_is_pack_or_tech_folder`, `_is_candidate_filename`) to filter out installers/patchers/injectors, optionally auto-extracting nested archives (`extract_archives_in_tree`, `_safe_zip_extract`/`_safe_tar_extract` guard against zip-slip via `_archive_member_safe`), then `add_games_to_steam` writes new shortcuts.
7. **Icon handling** — PE icon extraction (`extract_icon_from_pe_exe`), Steam grid image writes (`prepare_steam_icon`, `_collect_grid_files`), and screenshot capture (`capture_display_screenshot`, uses `ffmpeg` via `_ffmpeg_to_png`) — used by both "repair icons for existing games" and "set icon from screenshot".
8. **Running-game detection** — `find_running_nonsteam_game` parses Steam's own console log / process cmdline to figure out which non-Steam game (if any) is currently running, since deletion should warn/block while a game is active.
9. **CJK font/locale repair** — delegated to `cjk_font_repair.py` (lazy-loaded via `_load_cjk_font_repair()`, which searches several install-path candidates since the plugin can be deployed from different locations). It writes Proton prefix registry/locale settings plus `LANG`/`LC_ALL` launch options per language preset (`CJK_LANG_PRESETS`: `zh_CN`, `ja_JP`, `zh_TW`), and maps in a SimHei-equivalent font for Chinese.
10. **`class Plugin`** (bottom of `main.py`) — thin async wrappers around the above, one per RPC method; this is the only part directly reachable from the frontend, so business logic changes almost always belong in the free functions above it, not inline in a `Plugin` method.

### Deletion model (4 options, see README for exact Chinese labels)

Deleting a non-Steam game is parameterized by three independent booleans — body / saves / shader — combined into 4 preset options in both `src/index.tsx` (`OPTIONS` array) and mirrored server-side logic. "Saves" and "shader" both key off `compatdata/<appid>`/shader-cache lookups in `_collect_prefix_dirs`; deleting "saves" also destroys the Proton prefix (registry/config), not just save files — this is a known, documented (not accidental) side effect. `preview_delete` must be called before `delete_non_steam_game` in the UI flow so the confirmation dialog can list real, resolved paths (never delete based on unresolved/relative paths).

### Distinguishing "missing" vs "duplicate" shortcuts

These are separate cleanup features, not the same code path: `find_missing_nonsteam_games`/`purge_missing_nonsteam_games` handle shortcuts whose target exe no longer exists on disk (shortcut-only removal, never touches files); `find_duplicate_nonsteam_games`/`purge_duplicate_shortcuts` handle the same exe added more than once (keeps one shortcut, also file-safe).
