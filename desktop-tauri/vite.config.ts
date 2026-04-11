import { defineConfig } from 'vite';

export default defineConfig({
  clearScreen: false,
  test: {
    environment: 'jsdom',
  },
  server: {
    port: 1420,
    strictPort: true,
  },
});
