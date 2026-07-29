import React, { useState, useEffect, useCallback } from 'react';
import {
  definePlugin,
  ServerAPI,
  Panel,
  ButtonItem,
  Field,
} from 'decky-frontend-lib';

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

// 四个清理范围选项
const OPTIONS: DeleteOption[] = [
  { id: 'body', label: '删除本体', body: true, saves: false, shader: false },
  { id: 'body_saves', label: '删除本体 + 存档', body: true, saves: true, shader: false },
  { id: 'body_saves_shader', label: '删除本体 + 存档 + 着色器缓存', body: true, saves: true, shader: true },
  { id: 'body_shader', label: '删除本体 + 着色器缓存', body: true, saves: false, shader: true },
];

// 自定义全屏覆盖层（避免依赖有导出冲突的 Modal / ConfirmModal）
function Overlay({
  title,
  onClose,
  children,
}: {
  title?: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.72)',
        zIndex: 99999,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: '#1b2838',
          color: '#fff',
          borderRadius: 8,
          padding: 20,
          maxWidth: '92%',
          maxHeight: '92%',
          overflow: 'auto',
          boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
        }}
      >
        {title && <h3 style={{ marginTop: 0 }}>{title}</h3>}
        {children}
      </div>
    </div>
  );
}

function GamePanel({ serverApi }: { serverApi: ServerAPI }) {
  const [games, setGames] = useState<NonSteamGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [active, setActive] = useState<NonSteamGame | null>(null);
  const [pending, setPending] = useState<DeleteOption | null>(null);
  const [previewPaths, setPreviewPaths] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    serverApi
      .callPluginMethod('get_non_steam_games', {})
      .then((r: any) => {
        if (r.success) setGames(r.result || []);
        else setError('加载失败: ' + JSON.stringify(r.result));
      })
      .catch((e: any) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [serverApi]);

  useEffect(() => {
    load();
  }, [load]);

  const openOption = (game: NonSteamGame, opt: DeleteOption) => {
    setActive(game);
    setPending(opt);
    setResult(null);
    setBusy(true);
    serverApi
      .callPluginMethod('preview_delete', {
        appid: game.appid,
        userdata_id: game.userdata_id,
        exe: game.exe,
        start_dir: game.start_dir,
        delete_body: opt.body,
        delete_saves: opt.saves,
        delete_shader: opt.shader,
      })
      .then((r: any) => {
        if (r.success) setPreviewPaths((r.result && r.result.existing) || []);
        else setPreviewPaths([]);
      })
      .catch(() => setPreviewPaths([]))
      .finally(() => setBusy(false));
  };

  const doDelete = () => {
    if (!active || !pending) return;
    setBusy(true);
    serverApi
      .callPluginMethod('delete_non_steam_game', {
        appid: active.appid,
        userdata_id: active.userdata_id,
        key: active.key,
        exe: active.exe,
        start_dir: active.start_dir,
        delete_body: pending.body,
        delete_saves: pending.saves,
        delete_shader: pending.shader,
      })
      .then((r: any) => {
        if (r.success) {
          const d = r.result;
          setResult(
            `已删除 ${d.deleted.length} 项。\nSteam 快捷方式${d.removed_shortcut ? '已移除' : '未能移除(可能需手动编辑或重启 Steam)'}。\n请重启 Steam 使库列表更新。`
          );
        } else {
          setResult('删除失败: ' + JSON.stringify(r.result));
        }
      })
      .catch((e: any) => setResult('删除失败: ' + String(e)))
      .finally(() => {
        setBusy(false);
        setPending(null);
        load();
      });
  };

  const closeDialogs = () => {
    setPending(null);
    setActive(null);
    setPreviewPaths([]);
  };

  return (
    <div style={{ padding: '16px' }}>
      <Field label="非 Steam 游戏清理">
        <span>
          选择游戏与清理范围。删除前请先关闭该游戏。修改 shortcuts.vdf 后需重启 Steam 才能在库中消失。
        </span>
      </Field>

      {loading && <div>加载中...</div>}
      {error && <div style={{ color: '#f99' }}>{error}</div>}

      {!loading && !error && games.length === 0 && (
        <Field label="提示">
          <span>没有找到已添加的非 Steam 游戏。</span>
        </Field>
      )}

      {games.map((g) => (
        <Panel key={g.userdata_id + '_' + g.key}>
          <Field label={g.name || '(未命名)'}>
            <span>{'AppID: ' + g.appid}</span>
          </Field>
          <Field label="可执行文件">
            <span style={{ fontSize: '12px', wordBreak: 'break-all' }}>{g.exe}</span>
          </Field>
          {OPTIONS.map((opt) => (
            <ButtonItem key={opt.id} onClick={() => openOption(g, opt)}>
              {opt.label}
            </ButtonItem>
          ))}
        </Panel>
      ))}

      <ButtonItem onClick={load}>刷新列表</ButtonItem>

      {/* 二次确认弹窗 */}
      {pending && (
        <Overlay
          title={'确认：' + (pending ? pending.label : '')}
          onClose={closeDialogs}
        >
          <div>
            <p>即将删除以下项目（仅列出当前存在的）：</p>
            {busy && <p>正在计算...</p>}
            {!busy && previewPaths.length === 0 && (
              <p>没有找到可删除的文件（可能已不存在）。仍会尝试移除 Steam 快捷方式。</p>
            )}
            {!busy &&
              previewPaths.map((p, i) => (
                <div key={i} style={{ fontSize: '12px', wordBreak: 'break-all', color: '#fbb' }}>
                  {p}
                </div>
              ))}
            <p style={{ color: '#f99' }}>此操作不可恢复！</p>
            <div style={{ display: 'flex', gap: 12, marginTop: 12 }}>
              <button
                disabled={busy}
                onClick={doDelete}
                style={{
                  flex: 1,
                  background: '#a33',
                  color: '#fff',
                  border: 'none',
                  borderRadius: 4,
                  padding: '10px 12px',
                }}
              >
                确认删除
              </button>
              <button
                onClick={closeDialogs}
                style={{
                  flex: 1,
                  background: '#444',
                  color: '#fff',
                  border: 'none',
                  borderRadius: 4,
                  padding: '10px 12px',
                }}
              >
                取消
              </button>
            </div>
          </div>
        </Overlay>
      )}

      {/* 结果弹窗 */}
      {result && (
        <Overlay title="结果" onClose={() => setResult(null)}>
          <div style={{ fontSize: '13px', whiteSpace: 'pre-wrap', maxWidth: 460 }}>{result}</div>
          <div style={{ textAlign: 'right', marginTop: 12 }}>
            <button
              onClick={() => setResult(null)}
              style={{
                background: '#1a9fff',
                color: '#fff',
                border: 'none',
                borderRadius: 4,
                padding: '8px 16px',
              }}
            >
              关闭
            </button>
          </div>
        </Overlay>
      )}
    </div>
  );
}

export default definePlugin((serverApi: ServerAPI) => {
  return {
    title: <span>非Steam游戏清理</span>,
    icon: (
      <svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor">
        <path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" />
      </svg>
    ),
    content: <GamePanel serverApi={serverApi} />,
    onDismount: () => {},
  };
});
