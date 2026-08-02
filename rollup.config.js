import typescript from '@rollup/plugin-typescript';
import terser from '@rollup/plugin-terser';

/**
 * Decky Loader v3 默认用 ESMODULE_V1 加载插件：
 *   const plugin_exports = await import(.../dist/index.js);
 *   let plugin = plugin_exports.default();
 *
 * 因此必须输出 ES module，且 export default 为一个可调用函数。
 * SP_REACT / DFL 由 Steam/Decky 注入为全局，源码直接引用，禁止打包 React。
 */
export default {
  input: 'src/index.tsx',
  output: {
    file: 'dist/index.js',
    format: 'es',
    sourcemap: false,
  },
  // 不解析 react / decky-frontend-lib —— 源码已改用全局 SP_REACT / DFL
  plugins: [
    typescript({
      tsconfig: './tsconfig.json',
      // 声明了 SP_REACT 等全局，无需真实模块
      noEmitOnError: false,
    }),
    terser(),
  ],
};
