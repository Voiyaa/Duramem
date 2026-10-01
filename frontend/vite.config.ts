import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5174,
    strictPort: true,
    // 开发时把 /api 代理到后端；生产由后端直接托管 dist
    proxy: {
      '/api': { target: 'http://127.0.0.1:8001', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})