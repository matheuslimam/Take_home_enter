import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'

// troque "takehome-doc-extractor" pelo nome do repo no GitHub
export default defineConfig({
  plugins: [react()],
  base: '/Take_home_enter/'
})
