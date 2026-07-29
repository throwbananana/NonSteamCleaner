import typescript from '@rollup/plugin-typescript';
import resolve from '@rollup/plugin-node-resolve';
import commonjs from '@rollup/plugin-commonjs';
import terser from '@rollup/plugin-terser';

export default {
  input: 'src/index.tsx',
  output: {
    file: 'dist/index.js',
    format: 'iife',
    name: 'plugin',
    sourcemap: false,
  },
  plugins: [resolve(), commonjs(), typescript(), terser()],
};
