import { defineConfig } from "@playwright/test";

const previewBaseUrl = process.env.PREVIEW_BASE_URL;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  use: {
    baseURL: previewBaseUrl ?? "http://127.0.0.1:3000",
    trace: "retain-on-failure",
  },
  webServer: previewBaseUrl ? undefined : [
    {
      command: "uv run uvicorn movie_agent.main:app --host 127.0.0.1 --port 8000",
      cwd: "../backend",
      url: "http://127.0.0.1:8000/api/health",
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3000",
      cwd: ".",
      url: "http://127.0.0.1:3000",
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
