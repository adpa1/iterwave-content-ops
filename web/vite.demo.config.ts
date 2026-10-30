import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

/**
 * 单文件原型演示的构建配置：
 *   npx vite build --config vite.demo.config.ts
 * 产物 dist-demo/ 只有一份 JS + 一份 CSS（图片/字体全部内联），
 * 随后由 scripts/inline_single_html.py 合并成一个独立 HTML。
 */
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist-demo",
    emptyOutDir: true,
    cssCodeSplit: false,
    assetsInlineLimit: 1024 * 1024,
    reportCompressedSize: false,
    rollupOptions: {
      input: fileURLToPath(new URL("./demo.html", import.meta.url)),
      output: {
        inlineDynamicImports: true,
        entryFileNames: "demo.js",
        chunkFileNames: "demo-[name].js",
        assetFileNames: "demo.[ext]",
      },
    },
  },
});
