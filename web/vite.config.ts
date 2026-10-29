import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // 子路径部署：VITE_BASE=/iterwave/ npm run build → 资源与接口都带上 /iterwave 前缀
  base: process.env.VITE_BASE || "/",
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
