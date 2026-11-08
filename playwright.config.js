// @ts-check
const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:8123" },
  webServer: {
    command: "python3 -m http.server -d site 8123 --bind 127.0.0.1",
    url: "http://127.0.0.1:8123",
    reuseExistingServer: true,
    stdout: "ignore",
    stderr: "ignore",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
