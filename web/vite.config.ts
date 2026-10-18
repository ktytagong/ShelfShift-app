import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// npm run dev serves the UI on :5173 and forwards /api to the API. SS_API_ORIGIN picks the API (the browser test
// harness sets it to its own API child); without it, /api goes to `make serve` on :8080. vite preview inherits
// server.proxy. No compression plugin: it would buffer the API's streamed responses.
const api = process.env.SS_API_ORIGIN ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": api } },
});
