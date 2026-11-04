import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Lib mode: emit a single stable-named IIFE bundle at dist/widget.js.
// The companion dist/index.html comes from frontend/public/index.html
// (Vite copies the public dir verbatim into outDir).
//
// One artifact, two usage modes:
//   1. Direct visit to BE root  → BE serves index.html → loads /widget.js.
//   2. Bookmarklet on a 3rd-party page → injects <script src=".../widget.js">.
// Both run the exact same React + Chainlit-client + ChatPane code.
// @ts-expect-error vitest extends defineConfig at runtime
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: false,
    include: ["src/**/__tests__/**/*.{test,spec}.{ts,tsx}"],
  },
  // Recoil (pulled in by @chainlit/react-client) references
  // process.env.NODE_ENV at runtime. Vite doesn't shim `process` in lib
  // builds, so we inline the values it needs.
  define: {
    "process.env.NODE_ENV": JSON.stringify(process.env.VITEST ? "test" : "production"),
    "process.env": "{}",
  },
  build: {
    target: "es2020",
    outDir: "dist",
    emptyOutDir: true,
    lib: {
      entry: "src/widget.tsx",
      name: "VoittaWidget",
      formats: ["iife"],
      fileName: () => "widget.js",
    },
    rollupOptions: {
      // Bundle everything — the bookmarklet loads ONE file.
      output: { inlineDynamicImports: true },
    },
  },
});
