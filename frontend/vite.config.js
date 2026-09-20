import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The dashboard calls the Flask API. In development this proxy avoids
    // CORS entirely; in production set VITE_API_BASE_URL instead.
    proxy: {
      "/api": { target: "http://localhost:10000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
