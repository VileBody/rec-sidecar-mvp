import { expect, test } from "@playwright/test";

const viewports = [
  { name: "wide", width: 1710, height: 981 },
  { name: "medium", width: 1180, height: 981 },
  { name: "compact", width: 760, height: 981 },
];

for (const viewport of viewports) {
  test(`personal live ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/?mock=live");
    await expect(page.locator("#personalApp")).toHaveScreenshot(`personal-live-${viewport.name}.png`);
  });
}

test("personal wide preserves the three-column product geometry", async ({ page }) => {
  await page.setViewportSize({ width: 1710, height: 981 });
  await page.goto("/?mock=live");

  const expected = [
    { x: 16, y: 16, width: 630, height: 949 },
    { x: 663, y: 16, width: 560, height: 949 },
    { x: 1239, y: 16, width: 455, height: 949 },
  ];
  const actual = await page.locator("#personalApp > .column").evaluateAll((nodes) => nodes.map((node) => {
    const rect = node.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  }));

  expect(actual).toHaveLength(expected.length);
  actual.forEach((rect, index) => {
    for (const key of ["x", "y", "width", "height"]) {
      expect(Math.abs(rect[key] - expected[index][key]), `${index}.${key}`).toBeLessThanOrEqual(2);
    }
  });
});

test("personal login is account-only and has no registration", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 981 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Войти в REC Personal" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Создать аккаунт" })).toHaveCount(0);
  await expect(page).toHaveScreenshot("personal-auth.png");
});

test("recording controls expose both independent lanes", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 981 });
  await page.goto("/?mock=live");
  await page.getByRole("button", { name: "Включить всё" }).click();
  await expect(page.locator("#systemPill")).toHaveText("включено");
  await expect(page.locator("#microphonePill")).toHaveText("включено");
  await expect(page.getByText("можно свернуть окно", { exact: false })).toBeVisible();
});
