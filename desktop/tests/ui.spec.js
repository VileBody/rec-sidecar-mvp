import { expect, test } from "@playwright/test";

const viewports = [
  { name: "wide", width: 1710, height: 981 },
  { name: "medium", width: 1180, height: 981 },
  { name: "compact", width: 760, height: 981 },
];

for (const viewport of viewports) {
  test(`main ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/?mock=main");
    await expect(page.locator("#salesApp")).toHaveScreenshot(`main-${viewport.name}.png`);
  });
}

test("wide production geometry stays within two pixels", async ({ page }) => {
  await page.setViewportSize({ width: 1710, height: 981 });
  await page.goto("/?mock=active");

  const expected = [
    { x: 16, y: 16, width: 630, height: 949 },
    { x: 663, y: 16, width: 560, height: 949 },
    { x: 1239, y: 16, width: 455, height: 949 },
  ];
  const actual = await page.locator("#salesApp > .column").evaluateAll((nodes) => nodes.map((node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }));

  expect(actual).toHaveLength(expected.length);
  actual.forEach((rect, index) => {
    for (const key of ["x", "y", "width", "height"]) {
      expect(Math.abs(rect[key] - expected[index][key]), `${index}.${key}`).toBeLessThanOrEqual(2);
    }
  });
  await expect(page.locator("#salesApp")).toHaveScreenshot("active-wide.png");
});

test("auth, diagnostics and streaming/error states", async ({ page }) => {
  await page.setViewportSize({ width: 1710, height: 981 });
  await page.goto("/");
  await expect(page).toHaveScreenshot("auth-wide.png");

  await page.goto("/?mock=active");
  await page.getByRole("button", { name: "Диагностика" }).click();
  await expect(page.locator("#salesApp")) .toHaveScreenshot("diagnostics-wide.png");

  await page.goto("/?mock=streaming");
  await expect(page.locator("#salesApp")).toHaveScreenshot("streaming-wide.png");

  await page.goto("/?mock=error");
  await expect(page.locator("#salesApp")).toHaveScreenshot("error-wide.png");
});

test("reply window", async ({ page }) => {
  await page.setViewportSize({ width: 560, height: 380 });
  await page.goto("/pip.html");
  await expect(page).toHaveScreenshot("reply-window.png");
});
