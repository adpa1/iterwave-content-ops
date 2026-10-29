import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 部署在子路径时：VITE_BASE=/iterwave/ npm run build（默认根路径 /）
export default defineConfig({
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
