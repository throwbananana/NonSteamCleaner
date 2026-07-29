/*
 * 实验性：把清理选项注入 Steam 游戏属性里的“管理”选项卡。
 *
 * 默认【未启用】。原因如下：
 *   - Steam 客户端每次更新都会改变内部 React 组件的导出名与 props 结构，
 *     下列 'steamui/app-properties/app-properties' 与 props 取法需要按你的
 *     Steam 版本调整，否则不会显示或可能报错。
 *   - 主 UI（插件页面）已经是完整可用的清理入口，请优先使用它。
 *
 * 如需启用：
 *   1) 在 src/index.tsx 顶部加  import { setupManageTabPatch } from './patch';
 *   2) 在 definePlugin 回调里加  setupManageTabPatch(serverApi);
 *   3) 确认你的 decky 构建环境支持 'steamui/*' 模块解析（官方 decky-frontend-lib 支持）。
 *   4) 用 React DevTools / 浏览器控制台确认 AppProperties 的导出名与 appid 的 props 路径，
 *      并按需修改下方代码。
 *
 * 设计原则：失败时静默跳过，绝不阻断插件主页面，也绝不会误删真实 Steam 游戏的缓存。
 */

import { patch } from 'decky-frontend-lib';
import React from 'react';

export async function setupManageTabPatch(serverApi: any): Promise<boolean> {
  try {
    // 动态导入 Steam 内部组件；失败则静默跳过，不影响插件页面。
    const mod: any = await import('steamui/app-properties/app-properties');
    const AppProperties = mod && mod.AppProperties;
    if (!AppProperties) return false;

    patch(AppProperties, 'render', (original: any) => function (this: any, props: any, ...args: any[]) {
      const result = original.apply(this, [props, ...args]);
      try {
        // 尝试从多个可能的 props 位置取出 appid
        const appid =
          props?.appid ??
          props?.app?.appid ??
          props?.app?.getAppId?.() ??
          props?.selectedAppId ??
          (props?.app && props.app.appid);

        if (appid == null) return result;

        // 保险：仅在它是“非 Steam 快捷方式”时才注入。
        // 非 Steam 快捷方式的 appid 通常很大；这里做一个宽松判断，
        // 你也可以改为调用 serverApi.callPluginMethod('get_non_steam_games', {})
        // 拿到列表后做精确匹配。
        if (typeof appid !== 'number' || appid < 100000000) return result;

        const section = (
          <div
            style={{
              marginTop: '12px',
              borderTop: '1px solid #333',
              paddingTop: '8px',
            }}
          >
            <div style={{ marginBottom: '6px' }}>非 Steam 游戏清理（实验性）</div>
            <button
              onClick={() => {
                window.location.hash = '#/decky/plugin/NonSteamCleaner';
              }}
              style={{
                background: '#1a9fff',
                color: '#fff',
                border: 'none',
                borderRadius: '4px',
                padding: '8px 12px',
              }}
            >
              打开清理工具
            </button>
          </div>
        );

        if (result && result.props && Array.isArray(result.props.children)) {
          result.props.children.push(section);
        }
      } catch (e) {
        console.error('[NonSteamCleaner] patch error', e);
      }
      return result;
    });
    return true;
  } catch (e) {
    console.warn('[NonSteamCleaner] Manage 选项卡注入未生效（可忽略）:', e);
    return false;
  }
}
