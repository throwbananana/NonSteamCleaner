/**
 * NonSteamCleaner 前端 —— Decky Loader ESM v1
 *
 * 主要交互：在非 Steam 游戏的「库详情页 / 右键菜单 / 属性附近」注入清理选项
 * 插件面板仅作备用入口。
 */

declare const SP_REACT: typeof import('react');
declare const DFL: any;
declare const window: any;

const React = SP_REACT;

// ---------------------------------------------------------------------------
// Decky API
// ---------------------------------------------------------------------------
const manifest = { name: 'NonSteamCleaner' };
const API_VERSION = 2;
const internalAPIConnection =
  window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;

if (!internalAPIConnection) {
  throw new Error('[@NonSteamCleaner]: Decky Loader API 未初始化');
}

let api: any;
try {
  api = internalAPIConnection.connect(API_VERSION, manifest.name);
} catch {
  api = internalAPIConnection.connect(1, manifest.name);
}

const callable = api.callable as (name: string) => (...args: any[]) => Promise<any>;
const routerHook = api.routerHook;
const toaster = api.toaster;

const definePlugin = (fn: (...args: any[]) => any) => {
  return (...args: any[]) => fn(...args);
};

const getNonSteamGames = callable('get_non_steam_games');
const getGameByAppid = callable('get_game_by_appid');
const previewDelete = callable('preview_delete');
const deleteNonSteamGame = callable('delete_non_steam_game');
const getScanSettings = callable('get_scan_settings');
const setScanSettings = callable('set_scan_settings');
const scanDownloadGames = callable('scan_download_games');
const addNonSteamGames = callable('add_non_steam_games');
const findMissingNonsteamGames = callable('find_missing_nonsteam_games');
const purgeMissingNonsteamGames = callable('purge_missing_nonsteam_games');
const hideScanItems = callable('hide_scan_items');
const unhideScanItems = callable('unhide_scan_items');
const getHiddenScanItems = callable('get_hidden_scan_items');

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------
interface NonSteamGame {
  appid: number;
  name: string;
  exe: string;
  start_dir: string;
  userdata_id: string;
  key: string;
}

interface DeleteOption {
  id: string;
  label: string;
  body: boolean;
  saves: boolean;
  shader: boolean;
}

const OPTIONS: DeleteOption[] = [
  { id: 'body', label: '删除本体', body: true, saves: false, shader: false },
  { id: 'body_saves', label: '删除本体 + 存档', body: true, saves: true, shader: false },
  {
    id: 'body_saves_shader',
    label: '删除本体 + 存档 + 着色器缓存',
    body: true,
    saves: true,
    shader: true,
  },
  { id: 'body_shader', label: '删除本体 + 着色器缓存', body: true, saves: false, shader: true },
];

const LOG = (...a: any[]) => console.log('[NonSteamCleaner]', ...a);

// ---------------------------------------------------------------------------
// appid 工具：非 Steam 快捷方式最高位为 1
// ---------------------------------------------------------------------------
function normalizeAppId(v: any): number {
  let n = Number(v);
  if (!Number.isFinite(n)) return 0;
  n = n | 0; // int32
  return n >>> 0; // unsigned 32
}

function isNonSteamShortcutAppId(v: any): boolean {
  return normalizeAppId(v) >= 0x80000000;
}

function unwrapResult(r: any): any {
  if (r && typeof r === 'object' && 'result' in r && ('success' in r || r.result !== undefined)) {
    return r.result;
  }
  return r;
}

// ---------------------------------------------------------------------------
// 轻量 React 树工具 / afterPatch
// ---------------------------------------------------------------------------
function findInReactTree(node: any, pred: (n: any) => boolean, depth = 0): any {
  if (!node || depth > 40) return null;
  try {
    if (pred(node)) return node;
  } catch {
    /* ignore */
  }
  if (Array.isArray(node)) {
    for (const c of node) {
      const f = findInReactTree(c, pred, depth + 1);
      if (f) return f;
    }
    return null;
  }
  if (typeof node === 'object') {
    const kids = node.props?.children;
    if (kids !== undefined) {
      const f = findInReactTree(kids, pred, depth + 1);
      if (f) return f;
    }
  }
  return null;
}

function afterPatch(
  object: any,
  property: string,
  handler: (args: any[], ret: any) => any
): { unpatch: () => void } {
  if (!object || typeof object[property] !== 'function') {
    return { unpatch: () => {} };
  }
  const original = object[property];
  const patched = function (this: any, ...args: any[]) {
    const ret = original.apply(this, args);
    try {
      return handler.call(this, args, ret);
    } catch (e) {
      console.error('[NonSteamCleaner] afterPatch handler error', e);
      return ret;
    }
  };
  try {
    Object.assign(patched, original);
  } catch {
    /* ignore */
  }
  object[property] = patched;
  return {
    unpatch: () => {
      if (object[property] === patched) object[property] = original;
    },
  };
}

// ---------------------------------------------------------------------------
// 确认清理流程
// ---------------------------------------------------------------------------
async function resolveGame(appid: number, titleHint?: string): Promise<NonSteamGame | null> {
  try {
    const r = unwrapResult(await getGameByAppid({ appid: normalizeAppId(appid) }));
    if (r && r.appid != null) return r as NonSteamGame;
  } catch (e) {
    LOG('get_game_by_appid failed', e);
  }
  // 回退：拉全表匹配
  try {
    const list = unwrapResult(await getNonSteamGames());
    const arr = Array.isArray(list) ? list : [];
    const target = normalizeAppId(appid);
    const hit = arr.find((g: NonSteamGame) => normalizeAppId(g.appid) === target);
    if (hit) return hit;
    if (titleHint) {
      const byName = arr.find(
        (g: NonSteamGame) => String(g.name || '').trim() === String(titleHint).trim()
      );
      if (byName) return byName;
    }
  } catch (e) {
    LOG('get_non_steam_games failed', e);
  }
  return null;
}

function toast(title: string, body: string) {
  try {
    toaster?.toast?.({ title, body });
  } catch {
    console.log(title, body);
  }
}

function showConfirmModal(opts: {
  title: string;
  body: any;
  onConfirm: () => void | Promise<void>;
}) {
  const ConfirmModal = DFL.ConfirmModal;
  const showModal = DFL.showModal;
  if (typeof showModal === 'function' && ConfirmModal) {
    const modal = showModal(
      React.createElement(ConfirmModal, {
        strTitle: opts.title,
        strDescription: opts.body,
        strOKButtonText: '确认删除',
        strCancelButtonText: '取消',
        onOK: () => {
          void opts.onConfirm();
        },
        bDestructiveWarning: true,
      })
    );
    return modal;
  }
  // 回退：浏览器 confirm
  if (window.confirm(opts.title + '\n\n' + String(opts.body))) {
    void opts.onConfirm();
  }
}

/** Decky callable 推荐用「命名参数对象」，位置参数容易丢/错位导致全 false → 清除 0 项 */
function buildDeleteArgs(game: NonSteamGame, opt: DeleteOption) {
  return {
    appid: normalizeAppId(game.appid),
    userdata_id: String(game.userdata_id || ''),
    key: String(game.key ?? ''),
    exe: String(game.exe || ''),
    start_dir: String(game.start_dir || ''),
    delete_body: !!opt.body,
    delete_saves: !!opt.saves,
    delete_shader: !!opt.shader,
  };
}

async function runCleanupFlow(appid: number, opt: DeleteOption, titleHint?: string) {
  const game = await resolveGame(appid, titleHint);
  if (!game) {
    toast('非Steam清理', '未找到该非 Steam 游戏的快捷方式信息（可能不是非 Steam 游戏）。');
    return;
  }

  const args = buildDeleteArgs(game, opt);
  LOG('cleanup args', args);

  let preview: string[] = [];
  let normInfo = '';
  try {
    const r = unwrapResult(await previewDelete(args));
    preview = (r && r.existing) || [];
    if (r) {
      normInfo = `\n(解析路径 exe=${r.normalized_exe || '?'} start=${r.normalized_start || '?'})`;
    }
  } catch (e) {
    LOG('preview failed', e);
  }

  const lines =
    preview.length > 0
      ? preview.slice(0, 12).join('\n') + (preview.length > 12 ? `\n...共 ${preview.length} 项` : '')
      : '未找到可删除文件（可能路径带引号解析失败、文件已不存在，或 StartDir 过浅被保护）。仍会尝试移除 Steam 快捷方式。' +
        normInfo;

  showConfirmModal({
    title: `${opt.label}：${game.name || titleHint || game.appid}`,
    body: `将删除：\n${lines}\n\n此操作不可恢复！`,
    onConfirm: async () => {
      try {
        const r = unwrapResult(await deleteNonSteamGame(args));
        LOG('delete result raw', r);
        const n =
          typeof r?.count === 'number'
            ? r.count
            : Array.isArray(r?.deleted)
              ? r.deleted.length
              : 0;
        const scN = r?.removed_shortcut_count || (r?.removed_shortcut ? 1 : 0);
        const sc = r?.removed_shortcut
          ? `库快捷方式已移除(${scN}条)`
          : '库快捷方式未移除';
        const hint = r?.hint ? `\n${r.hint}` : '\n请完全退出 Steam 再打开以刷新库列表。';
        const failed =
          Array.isArray(r?.failed) && r.failed.length ? `\n其它: ${r.failed.slice(0, 2).join('; ')}` : '';
        toast(
          '非Steam清理',
          n > 0
            ? `已删除 ${n} 项文件/目录。${sc}。${hint}${failed}`
            : `未删除到文件(0 项)。${sc}。${hint}${failed}`
        );
      } catch (e) {
        toast('非Steam清理', '删除失败: ' + String(e));
      }
    },
  });
}

// ---------------------------------------------------------------------------
// 库详情页组件：注入到 /library/app/:appid
// ---------------------------------------------------------------------------
function GameCleanupPanel({ appId, title }: { appId: number; title?: string }) {
  const [known, setKnown] = React.useState<boolean | null>(null);
  const [gameName, setGameName] = React.useState(title || '');

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const g = await resolveGame(appId, title);
      if (cancelled) return;
      if (g) {
        setKnown(true);
        setGameName(g.name || title || '');
      } else {
        setKnown(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [appId, title]);

  // 不是非 Steam 或未找到：不渲染
  if (!isNonSteamShortcutAppId(appId)) return null;
  if (known === false) return null;
  if (known === null) {
    return React.createElement(
      'div',
      {
        style: {
          margin: '8px 12px',
          padding: '10px 12px',
          background: 'rgba(0,0,0,0.35)',
          borderRadius: 6,
          fontSize: 13,
        },
      },
      '正在检查非 Steam 游戏清理选项...'
    );
  }

  const ButtonItem = DFL.ButtonItem;
  const PanelSection = DFL.PanelSection || ((p: any) => React.createElement('div', null, p.children));
  const PanelSectionRow =
    DFL.PanelSectionRow || ((p: any) => React.createElement('div', { style: { marginBottom: 6 } }, p.children));

  return React.createElement(
    'div',
    {
      style: {
        margin: '8px 0 12px 0',
        padding: '4px 0',
      },
    },
    React.createElement(
      PanelSection,
      { title: '非 Steam 游戏清理' },
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'div',
          { style: { fontSize: 12, opacity: 0.85, marginBottom: 6 } },
          `针对「${gameName || appId}」的彻底清理。删除前请先关闭游戏。`
        )
      ),
      OPTIONS.map((opt) =>
        React.createElement(
          PanelSectionRow,
          { key: opt.id },
          ButtonItem
            ? React.createElement(
                ButtonItem,
                {
                  layout: 'below',
                  onClick: () => void runCleanupFlow(appId, opt, gameName),
                },
                opt.label
              )
            : React.createElement(
                'button',
                {
                  onClick: () => void runCleanupFlow(appId, opt, gameName),
                  style: {
                    width: '100%',
                    padding: '10px',
                    background: '#a33',
                    color: '#fff',
                    border: 'none',
                    borderRadius: 4,
                  },
                },
                opt.label
              )
        )
      )
    )
  );
}

function parseAppIdFromRouteArgs(args: any[]): number {
  try {
    const a0 = args?.[0];
    // 常见：props / match / params
    const candidates = [
      a0?.match?.params?.appid,
      a0?.params?.appid,
      a0?.appid,
      a0?.overview?.appid,
      a0?.app?.appid,
    ];
    for (const c of candidates) {
      const n = normalizeAppId(c);
      if (n) return n;
    }
  } catch {
    /* ignore */
  }
  // location 回退
  try {
    const m = String(window.location?.pathname || window.location?.hash || '').match(
      /\/library\/app\/(\d+)/
    );
    if (m) return normalizeAppId(m[1]);
  } catch {
    /* ignore */
  }
  return 0;
}

function ensureChildrenArray(node: any): any[] | null {
  if (!node || typeof node !== 'object') return null;
  const props = node.props;
  if (!props) return null;
  if (Array.isArray(props.children)) return props.children;
  if (props.children == null) {
    props.children = [];
    return props.children;
  }
  props.children = [props.children];
  return props.children;
}

function installLibraryAppPatch(): () => void {
  if (!routerHook || typeof routerHook.addPatch !== 'function') {
    LOG('routerHook.addPatch unavailable');
    return () => {};
  }

  const handle = routerHook.addPatch('/library/app/:appid', (props: any) => {
    try {
      if (!props || typeof props !== 'object') return props;
      const childNode = props.children;
      const childProps = childNode?.props;
      if (!childProps || typeof childProps.renderFunc !== 'function') return props;
      if ((childProps.renderFunc as any).__NSC_PATCHED) return props;

      const patch = afterPatch(childProps, 'renderFunc', (renderArgs, ret) => {
        if (!ret || typeof ret !== 'object') return ret;
        const appId = parseAppIdFromRouteArgs(renderArgs);
        if (!appId || !isNonSteamShortcutAppId(appId)) return ret;

        // 找一个合适的容器插入：优先 PlaySection / AppDetails 区域
        const host =
          findInReactTree(ret, (n) => {
            if (!n || typeof n !== 'object' || !n.props) return false;
            const cn = String(n.props.className || '');
            return (
              cn.includes('PlaySection') ||
              cn.includes('AppDetailSectionList') ||
              cn.includes('ActionButtonAndStatusPanel') ||
              cn.includes('AppDetailsContent')
            );
          }) || ret;

        const children = ensureChildrenArray(host);
        if (!children) return ret;

        // 去重
        for (let i = children.length - 1; i >= 0; i--) {
          const c = children[i];
          if (c && c.key === 'nsc-cleanup-panel') children.splice(i, 1);
        }

        const title =
          findInReactTree(ret, (n) => n && typeof n === 'object' && (n.display_name || n.name))
            ?.display_name ||
          findInReactTree(ret, (n) => n && typeof n === 'object' && n.props?.overview)?.props
            ?.overview?.display_name ||
          '';

        children.push(
          React.createElement(GameCleanupPanel, {
            key: 'nsc-cleanup-panel',
            appId,
            title: String(title || ''),
          })
        );
        return ret;
      });

      (childProps.renderFunc as any).__NSC_PATCHED = true;
      (childProps.renderFunc as any).__NSC_UNPATCH = patch.unpatch;
    } catch (e) {
      LOG('library patch error', e);
    }
    return props;
  });

  return () => {
    try {
      if (handle && typeof handle === 'function') handle();
      else if (handle?.unpatch) handle.unpatch();
      else if (routerHook.removePatch) routerHook.removePatch('/library/app/:appid', handle);
    } catch (e) {
      LOG('remove library patch failed', e);
    }
  };
}

// ---------------------------------------------------------------------------
// 游戏右键 / 选项菜单：在「属性」旁插入清理项
// ---------------------------------------------------------------------------
function isGamePropertiesMenu(items: any): boolean {
  if (!Array.isArray(items) || !items.length) return false;
  return Boolean(
    findInReactTree(items, (value) => {
      if (!value || typeof value !== 'object') return false;
      const props = value.props || {};
      const onSelected = props.onSelected || props.onClick;
      const key = String(value.key || '').toLowerCase();
      if (key === 'properties' || key.includes('propert')) return true;
      if (typeof onSelected === 'function') {
        const code = String(onSelected.toString());
        if (
          code.includes('AppProperties') ||
          code.includes('ShowAppProperties') ||
          code.includes('launchSource')
        )
          return true;
      }
      return false;
    })
  );
}

function extractAppIdFromMenu(items: any, context?: any): number {
  const tryVal = (v: any) => {
    const n = normalizeAppId(v);
    return n || 0;
  };

  // context / owner
  let n =
    tryVal(context?.appId) ||
    tryVal(context?.appid) ||
    tryVal(context?.overview?.appid) ||
    tryVal(context?._owner?.pendingProps?.overview?.appid);
  if (n) return n;

  const found = findInReactTree(items, (value) => {
    if (!value || typeof value !== 'object') return false;
    const appid =
      value.appid ??
      value.app?.appid ??
      value.overview?.appid ??
      value.props?.app?.appid ??
      value.props?.overview?.appid ??
      value._owner?.pendingProps?.overview?.appid ??
      value._owner?.pendingProps?.app?.appid;
    return Boolean(tryVal(appid));
  });
  if (found) {
    n =
      tryVal(found.appid) ||
      tryVal(found.app?.appid) ||
      tryVal(found.overview?.appid) ||
      tryVal(found.props?.app?.appid) ||
      tryVal(found.props?.overview?.appid) ||
      tryVal(found._owner?.pendingProps?.overview?.appid);
  }
  return n || 0;
}

function injectMenuItems(menuItems: any[], appId: number) {
  if (!Array.isArray(menuItems) || !appId || !isNonSteamShortcutAppId(appId)) return;
  if (!isGamePropertiesMenu(menuItems)) return;

  // 去重
  for (let i = menuItems.length - 1; i >= 0; i--) {
    const k = String(menuItems[i]?.key || '');
    if (k.startsWith('nsc-clean:')) menuItems.splice(i, 1);
  }

  const MenuItem = DFL.MenuItem;
  if (!MenuItem) return;

  const newItems = OPTIONS.map((opt) =>
    React.createElement(
      MenuItem,
      {
        key: 'nsc-clean:' + opt.id,
        tone: 'destructive',
        onSelected: () => {
          void runCleanupFlow(appId, opt);
        },
      },
      '清理: ' + opt.label.replace(/^删除/, '')
    )
  );

  // 插在「属性」前面
  let insertAt = menuItems.findIndex((item) => {
    return Boolean(
      findInReactTree(item, (value) => {
        if (!value || typeof value !== 'object') return false;
        const key = String(value.key || '').toLowerCase();
        if (key === 'properties') return true;
        const onSelected = value.props?.onSelected || value.props?.onClick;
        if (typeof onSelected === 'function') {
          const code = String(onSelected.toString());
          return code.includes('AppProperties') || code.includes('ShowAppProperties');
        }
        return false;
      })
    );
  });
  if (insertAt < 0) insertAt = menuItems.length;
  menuItems.splice(insertAt, 0, ...newItems);
}

function findWebpackModules(): any[] {
  const out: any[] = [];
  try {
    // 新版 steamui
    let req: any;
    // @ts-ignore
    if (window.webpackChunksteamui) {
      const id = Math.random();
      // @ts-ignore
      window.webpackChunksteamui.push([
        [id],
        {},
        (r: any) => {
          req = r;
        },
      ]);
    }
    if (req && req.c) {
      for (const k of Object.keys(req.c)) {
        try {
          const exp = req.c[k]?.exports;
          if (exp) out.push(exp);
        } catch {
          /* ignore */
        }
      }
    }
  } catch (e) {
    LOG('webpack scan failed', e);
  }
  return out;
}

function resolveLibraryContextMenuType(): any {
  // 优先用 DFL 查找
  try {
    if (typeof DFL.findModuleChild === 'function') {
      const found = DFL.findModuleChild((m: any) => {
        if (!m) return undefined;
        const s = String(m);
        if (s.includes('LibraryContextMenu') && typeof m === 'function') return m;
        return undefined;
      });
      if (found) return found;
    }
  } catch {
    /* ignore */
  }

  const modules = findWebpackModules();
  for (const exp of modules) {
    if (!exp || typeof exp !== 'object') continue;
    for (const v of Object.values(exp)) {
      if (typeof v !== 'function') continue;
      const s = String(v);
      if (s.includes('LibraryContextMenu')) return v;
    }
  }
  return null;
}

function installContextMenuPatch(): () => void {
  const unpatches: Array<() => void> = [];
  try {
    const LCM = resolveLibraryContextMenuType();
    if (!LCM || !LCM.prototype) {
      LOG('LibraryContextMenu not found — context menu inject skipped');
      return () => {};
    }
    if ((LCM as any).__NSC_CTX_PATCHED) return () => {};
    (LCM as any).__NSC_CTX_PATCHED = true;

    const outer = afterPatch(LCM.prototype, 'render', function (this: any, _args, ret) {
      try {
        // 从 this.props 取 overview
        const overview = this?.props?.overview || this?.props?.app;
        const appId = normalizeAppId(overview?.appid ?? overview?.appid);

        // 继续 patch 内部 type
        if (ret && typeof ret === 'object' && ret.type) {
          // 直接处理 children 里的菜单
          const children = ret.props?.children;
          if (Array.isArray(children)) {
            injectMenuItems(children, appId || extractAppIdFromMenu(children, this?.props));
          } else if (children && Array.isArray(children?.props?.children)) {
            injectMenuItems(
              children.props.children,
              appId || extractAppIdFromMenu(children.props.children, this?.props)
            );
          }

          // 深层：patch 返回组件的 render
          const innerType = ret.type;
          if (innerType?.prototype && !(innerType.prototype as any).__NSC_INNER) {
            (innerType.prototype as any).__NSC_INNER = true;
            const inner = afterPatch(innerType.prototype, 'render', function (this: any, _a, ret2) {
              try {
                const items =
                  ret2?.props?.children?.[0] ||
                  ret2?.props?.children ||
                  (Array.isArray(ret2?.props?.children) ? ret2.props.children : null);
                if (Array.isArray(items)) {
                  const id =
                    normalizeAppId(this?.props?.overview?.appid) ||
                    extractAppIdFromMenu(items, this?.props) ||
                    appId;
                  injectMenuItems(items, id);
                }
              } catch (e) {
                LOG('inner menu patch', e);
              }
              return ret2;
            });
            unpatches.push(inner.unpatch);
          }
        }
      } catch (e) {
        LOG('ctx render patch', e);
      }
      return ret;
    });
    unpatches.push(outer.unpatch);
    LOG('context menu patch installed');
  } catch (e) {
    LOG('installContextMenuPatch failed', e);
  }
  return () => {
    for (const u of unpatches) {
      try {
        u();
      } catch {
        /* ignore */
      }
    }
  };
}

// ---------------------------------------------------------------------------
// 插件面板：扫描 Downloads 并添加非 Steam 游戏
// ---------------------------------------------------------------------------
interface ScanCandidate {
  id: string;
  name: string;
  exe: string;
  start_dir: string;
  size: number;
  score: number;
  already_added: boolean;
  hidden?: boolean;
  rel_dir: string;
}

function formatSize(n: number): string {
  if (!n || n < 0) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
  return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

interface MissingGame {
  appid: number;
  name: string;
  exe: string;
  normalized_exe: string;
  start_dir: string;
  userdata_id: string;
  key: string;
  reason: string;
}

/** 简单错误边界：文件选择器返回后 SteamUI 偶发抛错时，避免整页白屏 */
class PanelErrorBoundary extends React.Component<
  { children: any },
  { error: string | null }
> {
  constructor(props: any) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(err: any) {
    return { error: String(err?.message || err || 'unknown') };
  }
  componentDidCatch(err: any, info: any) {
    LOG('PanelErrorBoundary', err, info);
  }
  render() {
    if (this.state.error) {
      return React.createElement(
        'div',
        { style: { padding: 16 } },
        React.createElement('div', { style: { color: '#f99', marginBottom: 12 } }, '界面出错: ' + this.state.error),
        React.createElement(
          'button',
          {
            onClick: () => this.setState({ error: null }),
            style: {
              padding: '10px 16px',
              background: '#1a9fff',
              color: '#fff',
              border: 'none',
              borderRadius: 4,
            },
          },
          '重试渲染'
        )
      );
    }
    return this.props.children;
  }
}

function PluginPanelInner() {
  const PanelSection = DFL.PanelSection || ((p: any) => React.createElement('div', null, p.children));
  const PanelSectionRow =
    DFL.PanelSectionRow || ((p: any) => React.createElement('div', { style: { marginBottom: 8 } }, p.children));
  const ButtonItem = DFL.ButtonItem;
  // 注意：DFL.TextField 在 openFilePicker 返回后容易触发 SteamUI 渲染崩溃，改用原生 input

  const [scanPath, setScanPath] = React.useState('/home/deck/Downloads');
  const [maxDepth, setMaxDepth] = React.useState(5);
  const [loading, setLoading] = React.useState(false);
  const [adding, setAdding] = React.useState(false);
  const [status, setStatus] = React.useState('');
  const [games, setGames] = React.useState<ScanCandidate[]>([]);
  const [hiddenGames, setHiddenGames] = React.useState<ScanCandidate[]>([]);
  const [selected, setSelected] = React.useState<Record<string, boolean>>({});
  const [hideAdded, setHideAdded] = React.useState(true);
  const [autoExtract, setAutoExtract] = React.useState(true);
  const [showHiddenBar, setShowHiddenBar] = React.useState(false);
  const [pathDraft, setPathDraft] = React.useState('/home/deck/Downloads');
  const [picking, setPicking] = React.useState(false);
  const [extractInfo, setExtractInfo] = React.useState('');

  // 失效游戏
  const [missing, setMissing] = React.useState<MissingGame[]>([]);
  const [missingStatus, setMissingStatus] = React.useState('');
  const [missingLoading, setMissingLoading] = React.useState(false);
  const [missingSelected, setMissingSelected] = React.useState<Record<string, boolean>>({});
  const [purging, setPurging] = React.useState(false);

  const applyPathSafe = React.useCallback((p: string) => {
    const next = String(p || '').trim() || '/home/deck/Downloads';
    // 延后到下一帧，等文件选择器完全关闭，避免与 SteamUI focus 恢复冲突
    window.setTimeout(() => {
      try {
        setPathDraft(next);
        setScanPath(next);
      } catch (e) {
        LOG('applyPathSafe', e);
      }
    }, 150);
  }, []);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = unwrapResult(await getScanSettings({})) || {};
        if (cancelled) return;
        if (s.scan_path) {
          setScanPath(s.scan_path);
          setPathDraft(s.scan_path);
        }
        if (s.max_depth) setMaxDepth(Number(s.max_depth) || 5);
        if (typeof s.auto_extract === 'boolean') setAutoExtract(s.auto_extract);
      } catch (e) {
        LOG('get_scan_settings', e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const visibleGames = React.useMemo(() => {
    return hideAdded ? games.filter((g) => !g.already_added) : games;
  }, [games, hideAdded]);

  const selectedCount = React.useMemo(() => {
    return visibleGames.filter((g) => selected[g.exe]).length;
  }, [visibleGames, selected]);

  const hiddenSelectedCount = React.useMemo(() => {
    return hiddenGames.filter((g) => selected[g.exe]).length;
  }, [hiddenGames, selected]);

  const missingKey = (m: MissingGame) =>
    String(m.userdata_id || '') + ':' + String(m.key || '') + ':' + String(m.appid || '');

  const missingSelectedCount = React.useMemo(() => {
    return missing.filter((m) => missingSelected[missingKey(m)]).length;
  }, [missing, missingSelected]);

  const doSavePath = async () => {
    const p = (pathDraft || '').trim() || '/home/deck/Downloads';
    try {
      const s = unwrapResult(
        await setScanSettings({
          scan_path: p,
          max_depth: maxDepth,
          auto_extract: autoExtract,
        })
      );
      const saved = s?.scan_path || p;
      setPathDraft(saved);
      setScanPath(saved);
      toast('扫描设置', '已保存: ' + saved + (autoExtract ? '（自动解压开）' : '（自动解压关）'));
    } catch (e) {
      toast('扫描设置', '保存失败: ' + String(e));
    }
  };

  const doScan = async () => {
    setLoading(true);
    setStatus(autoExtract ? '正在解压并扫描…（压缩包较多时会稍慢）' : '正在扫描…');
    setSelected({});
    setExtractInfo('');
    try {
      try {
        await setScanSettings({
          scan_path: pathDraft || scanPath,
          max_depth: maxDepth,
          auto_extract: autoExtract,
        });
      } catch {
        /* ignore */
      }
      const r = unwrapResult(
        await scanDownloadGames({
          scan_path: pathDraft || scanPath,
          max_depth: maxDepth,
          auto_extract: autoExtract,
          include_hidden: false,
        })
      );
      const list: ScanCandidate[] = (r && r.games) || [];
      const hiddenList: ScanCandidate[] = (r && r.hidden_games) || [];
      setGames(list);
      setHiddenGames(hiddenList);
      if (r?.scan_path) {
        setScanPath(r.scan_path);
        setPathDraft(r.scan_path);
      }
      const ex = r?.extract;
      if (ex && (ex.extracted_count || ex.failed_count || ex.skipped_existing)) {
        setExtractInfo(
          `解压：新${ex.extracted_count || 0} / 已存在跳过${ex.skipped_existing || 0} / 失败${ex.failed_count || 0}`
        );
      }
      const sel: Record<string, boolean> = {};
      for (const g of list) {
        if (!g.already_added && g.score >= 40) sel[g.exe] = true;
      }
      setSelected(sel);
      setStatus(r?.message || `扫描到 ${list.length} 个`);
      toast('扫描完成', r?.message || `找到 ${list.length} 个候选`);
    } catch (e) {
      setStatus('扫描失败: ' + String(e));
      toast('扫描失败', String(e));
    } finally {
      setLoading(false);
    }
  };

  const doHideSelected = async () => {
    const exes = visibleGames.filter((g) => selected[g.exe]).map((g) => g.exe);
    if (!exes.length) {
      toast('隐藏', '请先勾选要隐藏的启动项');
      return;
    }
    try {
      const r = unwrapResult(await hideScanItems({ exes }));
      toast('隐藏', `已隐藏 ${r?.added ?? exes.length} 项（可在隐藏栏找回）`);
      await doScan();
    } catch (e) {
      toast('隐藏失败', String(e));
    }
  };

  const doUnhideSelected = async () => {
    const exes = hiddenGames.filter((g) => selected[g.exe]).map((g) => g.exe);
    if (!exes.length) {
      toast('取消隐藏', '请在隐藏栏勾选要恢复的项');
      return;
    }
    try {
      const r = unwrapResult(await unhideScanItems({ exes }));
      toast('取消隐藏', `已恢复 ${r?.removed ?? exes.length} 项`);
      await doScan();
    } catch (e) {
      toast('取消隐藏失败', String(e));
    }
  };

  const toggleOne = (exe: string, value?: boolean) => {
    setSelected((prev) => ({
      ...prev,
      [exe]: typeof value === 'boolean' ? value : !prev[exe],
    }));
  };

  const selectAllVisible = (on: boolean) => {
    setSelected((prev) => {
      const next = { ...prev };
      for (const g of visibleGames) {
        if (!g.already_added) next[g.exe] = on;
      }
      return next;
    });
  };

  const doAdd = async () => {
    const pool = [...visibleGames, ...hiddenGames];
    // 去重 by exe
    const seen = new Set<string>();
    const entries = pool
      .filter((g) => {
        if (!selected[g.exe] || g.already_added) return false;
        if (seen.has(g.exe)) return false;
        seen.add(g.exe);
        return true;
      })
      .map((g) => ({
        name: g.name,
        exe: g.exe,
        start_dir: g.start_dir,
      }));
    if (!entries.length) {
      toast('添加游戏', '请先勾选要添加的游戏');
      return;
    }
    setAdding(true);
    setStatus(`正在添加 ${entries.length} 个…`);
    try {
      const r = unwrapResult(await addNonSteamGames({ entries }));
      setStatus(r?.message || '');
      toast('添加结果', r?.message || JSON.stringify(r));
      await doScan();
    } catch (e) {
      toast('添加失败', String(e));
      setStatus('添加失败: ' + String(e));
    } finally {
      setAdding(false);
    }
  };

  const pickFolder = async () => {
    if (picking) return;
    setPicking(true);
    const start = pathDraft || scanPath || '/home/deck';
    try {
      let res: any = null;
      // 与 Freedeck 一致：openFilePicker(FOLDER=1, start, includeFiles=false, includeFolders=true)
      if (typeof api.openFilePicker === 'function') {
        try {
          res = await api.openFilePicker(1, start, false, true);
        } catch (e1) {
          LOG('openFilePicker(1,...) failed', e1);
        }
      }
      if (!res && typeof api.openFilePickerV2 === 'function') {
        try {
          res = await api.openFilePickerV2(1, start, false, true);
        } catch (e2) {
          LOG('openFilePickerV2 failed', e2);
        }
      }
      const p = String(
        res?.realpath || res?.path || res?.result?.realpath || res?.result?.path || ''
      ).trim();
      if (p) {
        applyPathSafe(p);
        window.setTimeout(() => toast('扫描目录', '已选择: ' + p), 200);
      } else {
        toast('选择目录', '未选择（也可直接在输入框填写路径）');
      }
    } catch (e) {
      LOG('pickFolder', e);
      toast('选择目录', '取消或失败，请手动输入路径');
    } finally {
      // 延后解除，避免按钮立即重入 + focus 冲突
      window.setTimeout(() => setPicking(false), 300);
    }
  };

  const doFindMissing = async () => {
    setMissingLoading(true);
    setMissingStatus('正在检测…');
    try {
      const r = unwrapResult(await findMissingNonsteamGames({}));
      const list: MissingGame[] = (r && r.missing) || [];
      setMissing(list);
      const sel: Record<string, boolean> = {};
      for (const m of list) sel[missingKey(m)] = true;
      setMissingSelected(sel);
      setMissingStatus(r?.message || `失效 ${list.length} 个`);
      toast('失效检测', r?.message || `失效 ${list.length} 个`);
    } catch (e) {
      setMissingStatus('检测失败: ' + String(e));
      toast('失效检测', String(e));
    } finally {
      setMissingLoading(false);
    }
  };

  const doPurgeMissing = async (all: boolean) => {
    const entries = all
      ? missing
      : missing.filter((m) => missingSelected[missingKey(m)]);
    if (!entries.length) {
      toast('清理失效', '没有要移除的项，请先检测并勾选');
      return;
    }
    setPurging(true);
    try {
      const r = unwrapResult(
        await purgeMissingNonsteamGames(
          all ? { purge_all_missing: true } : { entries }
        )
      );
      toast('清理失效', r?.message || '完成');
      setMissingStatus(r?.message || '');
      await doFindMissing();
    } catch (e) {
      toast('清理失效', String(e));
    } finally {
      setPurging(false);
    }
  };

  const btnStyle = (primary?: boolean): any => ({
    width: '100%',
    padding: '10px 12px',
    marginTop: 4,
    background: primary ? '#1a9fff' : '#333',
    color: '#fff',
    border: 'none',
    borderRadius: 4,
  });

  return React.createElement(
    'div',
    { style: { padding: '0 0 16px 0' } },
    // -------- 失效清理 --------
    React.createElement(
      PanelSection,
      { title: '清理失效非 Steam 游戏' },
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'div',
          { style: { fontSize: 12, opacity: 0.9, lineHeight: 1.4 } },
          '自动检查库里非 Steam 快捷方式对应的启动文件是否还在磁盘上。若不存在，可从 Steam 库中移除快捷方式（不会删除其它文件）。'
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        ButtonItem
          ? React.createElement(
              ButtonItem,
              { layout: 'below', onClick: () => void doFindMissing() },
              missingLoading ? '检测中…' : '检测失效游戏'
            )
          : React.createElement(
              'button',
              { style: btnStyle(true), disabled: missingLoading, onClick: () => void doFindMissing() },
              missingLoading ? '检测中…' : '检测失效游戏'
            )
      ),
      missingStatus
        ? React.createElement(
            PanelSectionRow,
            null,
            React.createElement('div', { style: { fontSize: 12, color: '#9cf' } }, missingStatus)
          )
        : null,
      missing.length > 0
        ? React.createElement(
            React.Fragment,
            null,
            React.createElement(
              PanelSectionRow,
              null,
              React.createElement(
                'div',
                { style: { display: 'flex', gap: 8 } },
                React.createElement(
                  'button',
                  {
                    style: { ...btnStyle(), flex: 1 },
                    onClick: () => {
                      const sel: Record<string, boolean> = {};
                      for (const m of missing) sel[missingKey(m)] = true;
                      setMissingSelected(sel);
                    },
                  },
                  '全选'
                ),
                React.createElement(
                  'button',
                  {
                    style: { ...btnStyle(), flex: 1 },
                    onClick: () => setMissingSelected({}),
                  },
                  '全不选'
                )
              )
            ),
            ...missing.map((m) =>
              React.createElement(
                PanelSectionRow,
                { key: missingKey(m) },
                React.createElement(
                  'div',
                  {
                    style: {
                      display: 'flex',
                      gap: 10,
                      alignItems: 'flex-start',
                      padding: '6px 0',
                      borderBottom: '1px solid rgba(255,255,255,0.08)',
                    },
                  },
                  React.createElement('input', {
                    type: 'checkbox',
                    checked: !!missingSelected[missingKey(m)],
                    onChange: (e: any) =>
                      setMissingSelected((prev) => ({
                        ...prev,
                        [missingKey(m)]: !!e.target.checked,
                      })),
                    style: { marginTop: 4, width: 18, height: 18 },
                  }),
                  React.createElement(
                    'div',
                    { style: { flex: 1, minWidth: 0 } },
                    React.createElement(
                      'div',
                      { style: { fontWeight: 600, fontSize: 14, color: '#fbb' } },
                      m.name || '(未命名)',
                      React.createElement(
                        'span',
                        { style: { marginLeft: 8, fontSize: 11, opacity: 0.8 } },
                        m.reason
                      )
                    ),
                    React.createElement(
                      'div',
                      { style: { fontSize: 11, opacity: 0.75, wordBreak: 'break-all' } },
                      m.normalized_exe || m.exe || '(无路径)'
                    )
                  )
                )
              )
            ),
            React.createElement(
              PanelSectionRow,
              null,
              ButtonItem
                ? React.createElement(
                    ButtonItem,
                    {
                      layout: 'below',
                      onClick: () => void doPurgeMissing(false),
                    },
                    purging ? '移除中…' : `从 Steam 移除所选 (${missingSelectedCount})`
                  )
                : React.createElement(
                    'button',
                    {
                      style: btnStyle(true),
                      disabled: purging || missingSelectedCount === 0,
                      onClick: () => void doPurgeMissing(false),
                    },
                    purging ? '移除中…' : `从 Steam 移除所选 (${missingSelectedCount})`
                  )
            ),
            React.createElement(
              PanelSectionRow,
              null,
              ButtonItem
                ? React.createElement(
                    ButtonItem,
                    { layout: 'below', onClick: () => void doPurgeMissing(true) },
                    purging ? '移除中…' : `一键移除全部失效 (${missing.length})`
                  )
                : React.createElement(
                    'button',
                    {
                      style: { ...btnStyle(), background: '#a33' },
                      disabled: purging,
                      onClick: () => void doPurgeMissing(true),
                    },
                    purging ? '移除中…' : `一键移除全部失效 (${missing.length})`
                  )
            )
          )
        : null
    ),
    // -------- 扫描添加 --------
    React.createElement(
      PanelSection,
      { title: '扫描并添加非 Steam 游戏' },
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'div',
          { style: { fontSize: 12, opacity: 0.9, lineHeight: 1.4 } },
          '识别目录下子文件夹中的启动程序，勾选后写入 Steam 库。添加后请重启 Steam。'
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'div',
          { style: { fontSize: 12, marginBottom: 6 } },
          '扫描目录（默认可改，也可用浏览）'
        ),
        // 原生 input，避免 DFL.TextField + 文件选择器返回后的 SteamUI 崩溃
        React.createElement('input', {
          type: 'text',
          value: pathDraft,
          onChange: (e: any) => setPathDraft(String(e?.target?.value ?? '')),
          style: {
            width: '100%',
            boxSizing: 'border-box',
            padding: '10px 8px',
            background: '#1b2838',
            color: '#fff',
            border: '1px solid #456',
            borderRadius: 4,
            fontSize: 13,
          },
        }),
        React.createElement(
          'div',
          {
            style: {
              fontSize: 11,
              opacity: 0.65,
              marginTop: 4,
              wordBreak: 'break-all',
            },
          },
          '当前: ' + (pathDraft || scanPath || '(空)')
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'button',
          {
            style: btnStyle(),
            disabled: picking,
            onClick: () => void pickFolder(),
          },
          picking ? '选择中…' : '浏览文件夹…'
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'button',
          { style: btnStyle(), onClick: () => void doSavePath() },
          '保存为默认扫描目录'
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'button',
          {
            style: btnStyle(true),
            disabled: loading,
            onClick: () => void doScan(),
          },
          loading ? '扫描中…' : '开始扫描'
        )
      ),
      status
        ? React.createElement(
            PanelSectionRow,
            null,
            React.createElement('div', { style: { fontSize: 12, color: '#9cf' } }, status)
          )
        : null,
      extractInfo
        ? React.createElement(
            PanelSectionRow,
            null,
            React.createElement('div', { style: { fontSize: 12, color: '#fc6' } }, extractInfo)
          )
        : null,
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'label',
          { style: { fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 } },
          React.createElement('input', {
            type: 'checkbox',
            checked: autoExtract,
            onChange: (e: any) => setAutoExtract(!!e.target.checked),
          }),
          '扫描时自动解压压缩包（zip/7z/rar/tar，可递归）'
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'label',
          { style: { fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 } },
          React.createElement('input', {
            type: 'checkbox',
            checked: hideAdded,
            onChange: (e: any) => setHideAdded(!!e.target.checked),
          }),
          '隐藏已在库中的'
        )
      )
    ),
    React.createElement(
      PanelSection,
      { title: `候选列表 (${visibleGames.length})` },
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'div',
          { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
          React.createElement(
            'button',
            { style: { ...btnStyle(), flex: 1 }, onClick: () => selectAllVisible(true) },
            '全选'
          ),
          React.createElement(
            'button',
            { style: { ...btnStyle(), flex: 1 }, onClick: () => selectAllVisible(false) },
            '全不选'
          )
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'button',
          {
            style: btnStyle(),
            disabled: selectedCount === 0,
            onClick: () => void doHideSelected(),
          },
          `放入隐藏栏 (${selectedCount})`
        )
      ),
      visibleGames.length === 0
        ? React.createElement(
            PanelSectionRow,
            null,
            React.createElement(
              'div',
              { style: { opacity: 0.7, fontSize: 13 } },
              loading ? '扫描中…' : '暂无结果，请先扫描'
            )
          )
        : null,
      ...visibleGames.map((g) =>
        React.createElement(
          PanelSectionRow,
          { key: g.exe },
          React.createElement(
            'div',
            {
              style: {
                display: 'flex',
                gap: 10,
                alignItems: 'flex-start',
                padding: '8px 4px',
                borderBottom: '1px solid rgba(255,255,255,0.08)',
                opacity: g.already_added ? 0.55 : 1,
              },
              onClick: () => !g.already_added && toggleOne(g.exe),
            },
            React.createElement('input', {
              type: 'checkbox',
              checked: !!selected[g.exe],
              disabled: g.already_added,
              onChange: (e: any) => {
                e.stopPropagation?.();
                toggleOne(g.exe, !!e.target.checked);
              },
              onClick: (e: any) => e.stopPropagation?.(),
              style: { marginTop: 4, width: 18, height: 18 },
            }),
            React.createElement(
              'div',
              { style: { flex: 1, minWidth: 0 } },
              React.createElement(
                'div',
                { style: { fontWeight: 600, fontSize: 14 } },
                g.name,
                g.already_added
                  ? React.createElement(
                      'span',
                      { style: { marginLeft: 8, fontSize: 11, color: '#8f8' } },
                      '已在库中'
                    )
                  : null
              ),
              React.createElement(
                'div',
                {
                  style: {
                    fontSize: 11,
                    opacity: 0.75,
                    wordBreak: 'break-all',
                    marginTop: 2,
                  },
                },
                g.rel_dir || '.',
                ' / ',
                g.exe.split('/').pop(),
                g.size ? ` · ${formatSize(g.size)}` : ''
              )
            )
          )
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'button',
          {
            style: {
              ...btnStyle(true),
              background: selectedCount ? '#1a9fff' : '#444',
            },
            disabled: adding || selectedCount === 0,
            onClick: () => void doAdd(),
          },
          adding ? '添加中…' : `添加所选到 Steam (${selectedCount})`
        )
      )
    ),
    // -------- 隐藏栏 --------
    React.createElement(
      PanelSection,
      { title: `隐藏栏 (${hiddenGames.length})` },
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'div',
          { style: { fontSize: 12, opacity: 0.9, lineHeight: 1.4 } },
          '放入隐藏栏的启动项下次扫描默认不再出现，可在此找回并取消隐藏。'
        )
      ),
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'label',
          { style: { fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 } },
          React.createElement('input', {
            type: 'checkbox',
            checked: showHiddenBar,
            onChange: (e: any) => setShowHiddenBar(!!e.target.checked),
          }),
          '展开隐藏栏'
        )
      ),
      showHiddenBar
        ? React.createElement(
            React.Fragment,
            null,
            hiddenGames.length === 0
              ? React.createElement(
                  PanelSectionRow,
                  null,
                  React.createElement(
                    'div',
                    { style: { opacity: 0.7, fontSize: 13 } },
                    '隐藏栏为空（先扫描，再在候选列表勾选后点「放入隐藏栏」）'
                  )
                )
              : null,
            ...hiddenGames.map((g) =>
              React.createElement(
                PanelSectionRow,
                { key: 'hid-' + g.exe },
                React.createElement(
                  'div',
                  {
                    style: {
                      display: 'flex',
                      gap: 10,
                      alignItems: 'flex-start',
                      padding: '6px 0',
                      borderBottom: '1px solid rgba(255,255,255,0.08)',
                    },
                  },
                  React.createElement('input', {
                    type: 'checkbox',
                    checked: !!selected[g.exe],
                    onChange: (e: any) => toggleOne(g.exe, !!e.target.checked),
                    style: { marginTop: 4, width: 18, height: 18 },
                  }),
                  React.createElement(
                    'div',
                    { style: { flex: 1, minWidth: 0 } },
                    React.createElement(
                      'div',
                      { style: { fontWeight: 600, fontSize: 13 } },
                      g.name
                    ),
                    React.createElement(
                      'div',
                      {
                        style: {
                          fontSize: 11,
                          opacity: 0.7,
                          wordBreak: 'break-all',
                        },
                      },
                      g.exe
                    )
                  )
                )
              )
            ),
            React.createElement(
              PanelSectionRow,
              null,
              React.createElement(
                'button',
                {
                  style: btnStyle(true),
                  disabled: hiddenSelectedCount === 0,
                  onClick: () => void doUnhideSelected(),
                },
                `取消隐藏所选 (${hiddenSelectedCount})`
              )
            ),
            React.createElement(
              PanelSectionRow,
              null,
              React.createElement(
                'button',
                {
                  style: btnStyle(),
                  disabled: hiddenSelectedCount === 0,
                  onClick: () => void doAdd(),
                },
                `添加隐藏栏所选到 Steam`
              )
            )
          )
        : React.createElement(
            PanelSectionRow,
            null,
            React.createElement(
              'div',
              { style: { fontSize: 12, opacity: 0.65 } },
              hiddenGames.length
                ? `有 ${hiddenGames.length} 项在隐藏栏，勾选「展开隐藏栏」查看`
                : '暂无隐藏项'
            )
          )
    ),
    React.createElement(
      PanelSection,
      { title: '清理说明' },
      React.createElement(
        PanelSectionRow,
        null,
        React.createElement(
          'div',
          { style: { fontSize: 12, opacity: 0.85, lineHeight: 1.45 } },
          '游戏仍在磁盘时：打开非 Steam 游戏详情页/菜单可删除本体与存档。',
          React.createElement('br'),
          '文件已删但库里还在：用上方「清理失效非 Steam 游戏」。改完后请完全退出 Steam 再打开。',
          React.createElement('br'),
          '压缩包：开启自动解压后，会解到同名文件夹再识别启动器（已解压过的会跳过）。'
        )
      )
    )
  );
}

function PluginPanel() {
  return React.createElement(
    PanelErrorBoundary,
    null,
    React.createElement(PluginPanelInner, null)
  );
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------
const unpatchFns: Array<() => void> = [];

const index = definePlugin(() => {
  try {
    unpatchFns.push(installLibraryAppPatch());
  } catch (e) {
    LOG('library patch init failed', e);
  }
  // 延迟一点再装菜单补丁，等 Steam UI webpack 就绪
  const t = window.setTimeout(() => {
    try {
      unpatchFns.push(installContextMenuPatch());
    } catch (e) {
      LOG('ctx patch init failed', e);
    }
  }, 2500);

  return {
    name: 'NonSteamCleaner',
    titleView: React.createElement(
      'div',
      { className: DFL.staticClasses?.Title },
      '非Steam游戏'
    ),
    content: React.createElement(PluginPanel, null),
    icon: React.createElement(
      'svg',
      { viewBox: '0 0 24 24', width: 24, height: 24, fill: 'currentColor' },
      React.createElement('path', {
        d: 'M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z',
      })
    ),
    alwaysRender: true,
    onDismount() {
      window.clearTimeout(t);
      while (unpatchFns.length) {
        const fn = unpatchFns.pop();
        try {
          fn && fn();
        } catch {
          /* ignore */
        }
      }
    },
  };
});

export default index;
