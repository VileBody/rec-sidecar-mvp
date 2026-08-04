import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:4174",
    browserName: "chromium",
    channel: "chrome",
    colorScheme: "light",
    locale: "ru-RU",
    screenshot: "only-on-failure",
  },
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      caret: "hide",
      maxDiffPixelRatio: 0.01,
      scale: "css",
    },
  },
  webServer: {
    command: "./scripts/prepare-ui.sh && python3 -m http.server 4174 --directory dist",
    port: 4174,
    reuseExistingServer: true,
  },
});
