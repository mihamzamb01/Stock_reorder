import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // `npm run dev` proxies API calls to the Python dashboard server.
    proxy: {
      "/api": "http://localhost:8642",
    },
  },
});
