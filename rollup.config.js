import typescript from '@rollup/plugin-typescript';
import resolve from '@rollup/plugin-node-resolve';
import commonjs from '@rollup/plugin-commonjs';
import replace from '@rollup/plugin-replace';
import terser from '@rollup/plugin-terser';

export default {
  input: 'src/index.tsx',
  output: {
    file: 'dist/index.js',
    format: 'iife',
    name: 'plugin',
    sourcemap: false,
    // Steam 的浏览器环境没有 Node 的 process，提供 shim 作为兜底
    banner: 'var process = process || { env: { NODE_ENV: "production" } };',
  },
  plugins: [
    // react / decky-frontend-lib 会读取 process.env.NODE_ENV，必须内联替换掉
    replace({
      preventAssignment: true,
      values: { 'process.env.NODE_ENV': JSON.stringify('production') },
    }),
    resolve(),
    commonjs(),
    typescript(),
    terser(),
  ],
};
