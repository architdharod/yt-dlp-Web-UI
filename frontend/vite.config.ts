import path from "path";
// vitest/config re-exports Vite's defineConfig with the `test` block typed.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    // Registers Testing Library's afterEach(cleanup); see src/test-setup.ts.
    setupFiles: ["./src/test-setup.ts"],
  },
});
