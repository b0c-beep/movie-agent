import { expect, test } from "@playwright/test";

test("unlocks the app and displays streamed grounded recommendations", async ({ page }) => {
  await page.route("**/api/auth/session", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"locked"}' }),
  );
  await page.route("**/api/auth/unlock", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        session_id: "fixture-session",
        expires_at: "2030-01-01T00:00:00Z",
      }),
    }),
  );
  await page.route("**/api/recommendations/stream", (route) => {
    const runId = "fixture-run";
    const now = "2026-10-25T12:00:00Z";
    const event = (
      sequence: number,
      type: string,
      stage: string,
      summary: string,
      data?: Record<string, unknown>,
    ) =>
      `id: ${sequence}\nevent: ${type}\ndata: ${JSON.stringify({
        event_id: `fixture-event-${sequence}`,
        run_id: runId,
        sequence,
        stage,
        type,
        origin: "application",
        summary,
        timestamp: now,
        metadata: {},
        data,
      })}\n\n`;
    const recommendation = {
      rank: 1,
      match_reason: "TMDB lists this as science fiction.",
      matched_constraints: ["genre:Science Fiction"],
      caveats: [],
      movie: {
        tmdb_id: 603,
        title: "The Matrix",
        release_year: 1999,
        runtime_minutes: 136,
        genres: ["Science Fiction"],
        overview: "A hacker discovers a simulated world.",
        poster_url: null,
        tmdb_url: "https://www.themoviedb.org/movie/603",
        imdb_url: null,
        ratings: [{ source: "tmdb", value: 8.2, scale: "10", vote_count: 100 }],
        watch_options: {
          country: "RO",
          subscription: [{ provider_id: 8, name: "Netflix", logo_path: "/netflix.jpg" }],
          free: [],
          ads: [],
          rent: [],
          buy: [],
        },
      },
    };
    const frames = [
      event(1, "run_started", "predict", "Starting a protected movie search"),
      event(2, "recommendations", "complete", "Prepared one grounded recommendation", {
        intent: { requested_count: 1 },
        recommendations: [recommendation],
        requested_count: 1,
        shortfall_reason: null,
        issues: [],
        provider_lookups: [],
      }),
      event(3, "completed", "complete", "Recommendation run completed"),
    ].join("");
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: {
        "x-conversation-id": "fixture-conversation",
        "x-run-id": runId,
      },
      body: frames,
    });
  });
  await page.route("**/api/recommendations/conversations/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        conversation_id: "fixture-conversation",
        version: 2,
        intent: { requested_count: 1 },
        result: null,
      }),
    }),
  );
  await page.goto("/");
  await page.getByLabel("Passcode").fill("fixture-passcode");
  await page.getByRole("button", { name: "Enter" }).click();
  await page.getByLabel("Tell me what you're in the mood for").fill("One science fiction movie");
  await page.getByRole("button", { name: "Get recommendations" }).click();

  await expect(page.getByTestId("status")).toHaveText("Complete");
  await expect(page.getByRole("heading", { name: "The Matrix" })).toBeVisible();
  await expect(page.locator(".movieRating")).toContainText("8.2 / 10");
  await expect(page.locator(".providerBadge img")).toHaveAttribute(
    "src",
    "https://image.tmdb.org/t/p/w92/netflix.jpg",
  );
  await expect(page.locator(".trace li")).toHaveCount(3);
});

test("shows streamed failures and keeps the saved conversation version", async ({ page }) => {
  const savedConversation = { id: "fixture-conversation", version: 3 };
  await page.addInitScript((conversation) => {
    window.localStorage.setItem("movie-agent-conversation", JSON.stringify(conversation));
  }, savedConversation);
  await page.route("**/api/auth/session", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"locked"}' }),
  );
  await page.route("**/api/auth/unlock", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        session_id: "fixture-session",
        expires_at: "2030-01-01T00:00:00Z",
      }),
    }),
  );
  await page.route("**/api/recommendations/conversations/fixture-conversation", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        conversation_id: savedConversation.id,
        version: savedConversation.version,
        intent: {},
        result: null,
      }),
    }),
  );
  await page.route("**/api/recommendations/stream", (route) => {
    const event = (sequence: number, type: string, summary: string, metadata = {}) =>
      `id: ${sequence}\nevent: ${type}\ndata: ${JSON.stringify({
        event_id: `failed-event-${sequence}`,
        run_id: "fixture-failed-run",
        sequence,
        stage: type === "failed" ? "complete" : "predict",
        type,
        origin: "application",
        summary,
        timestamp: "2026-10-25T12:00:00Z",
        metadata,
      })}\n\n`;
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: {
        "x-conversation-id": savedConversation.id,
        "x-run-id": "fixture-failed-run",
      },
      body: [
        event(1, "run_started", "Starting a protected movie search"),
        event(2, "failed", "The recommendation run could not be completed", {
          error_code: "recommendation_failed",
          failure_stage: "intent_extraction",
          exception_type: "UnexpectedModelBehavior",
        }),
      ].join(""),
    });
  });

  await page.goto("/");
  await page.getByLabel("Passcode").fill("fixture-passcode");
  await page.getByRole("button", { name: "Enter" }).click();
  await expect(page.getByRole("button", { name: "New search" })).toBeVisible();
  await page.getByLabel("Tell me what you're in the mood for").fill("three comedies");
  await page.getByRole("button", { name: "Get recommendations" }).click();

  await expect(page.getByTestId("status")).toHaveText("Failed");
  await expect(page.locator(".trace [role=alert]")).toContainText("couldn’t interpret that request");
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("movie-agent-conversation")))
    .toBe(JSON.stringify(savedConversation));
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
