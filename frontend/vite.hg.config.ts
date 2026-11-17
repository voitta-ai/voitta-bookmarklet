// The hg.voitta.ai panel: a small separate bundle (dist/hg-panel.js) the Hedgehog bookmarklet
// fetches and runs on app.stocktrak.com. Built after widget.js (emptyOutDir: false keeps it).
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
  },
  build: {
    target: "es2020",
    outDir: "dist",
    emptyOutDir: false,
    lib: {
      entry: "src/hg/panel.tsx",
      name: "VoittaHgPanel",
      formats: ["iife"],
      fileName: () => "hg-panel.js",
    },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
