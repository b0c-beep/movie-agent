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

test("delivers an event before the stream completes", async ({ page }) => {
  await page.goto("/");

  const observed = await page.evaluate(async () => {
    const response = await fetch("/api/fixture-stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ prompt: "A comedy" }),
    });
    if (!response.ok || !response.body) throw new Error("Fixture stream unavailable");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let text = "";
    let completedBeforeFirstEvent = false;
    let sawFirstEvent = false;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
      if (!sawFirstEvent && text.includes("event: run_started")) {
        sawFirstEvent = true;
        completedBeforeFirstEvent = text.includes("event: completed");
      }
    }
    text += decoder.decode();
    return {
      sawFirstEvent,
      completedBeforeFirstEvent,
      sawCompletion: text.includes("event: completed"),
    };
  });

  expect(observed.sawFirstEvent).toBe(true);
  expect(observed.completedBeforeFirstEvent).toBe(false);
  expect(observed.sawCompletion).toBe(true);
});
