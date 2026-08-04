import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./personal-tests",
  outputDir: "./test-results/personal",
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:4175",
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
    command: "./scripts/prepare-personal-ui.sh && python3 -m http.server 4175 --directory personal-dist",
    port: 4175,
    reuseExistingServer: true,
  },
});
