import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/dashboard/', // 👈 This makes sure the browser looks for /dashboard/assets/...
})
