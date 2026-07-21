import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 代理目标可配置：默认指向本机 8000（本地开发）。
// 远程开发时（前端在 A 机、后端在 Ubuntu）可用环境变量覆盖，例如：
//   VITE_API_TARGET=http://10.0.0.5:8000 npm run dev
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    // 监听所有网卡，允许从其他机器通过 IP 访问 dev server（远程调试必需）
    host: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
