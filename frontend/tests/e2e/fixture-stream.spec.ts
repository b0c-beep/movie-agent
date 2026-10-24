import { expect, test } from "@playwright/test";

test("streams fixture events through the same-origin API route", async ({ page, context }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Run fixture" }).click();

  await expect(page.getByTestId("status")).toHaveText("Complete");
  await expect(page.locator(".trace li")).toHaveCount(5);
  await expect(page.locator(".trace li").last()).toContainText("Fixture stream completed");

  const cookies = await context.cookies();
  const transportCookie = cookies.find((cookie) => cookie.name === "fixture_transport");
  expect(transportCookie?.value).toBe("verified");
  expect(transportCookie?.httpOnly).toBe(true);
});
