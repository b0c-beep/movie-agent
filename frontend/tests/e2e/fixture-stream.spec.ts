import { expect, test } from "@playwright/test";

test("unlocks the app and displays streamed grounded recommendations", async ({ page }) => {
  let streamCalls = 0;
  const availabilityRequests: { country: string; expected_version: number }[] = [];
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
  await page.route("**/api/recommendations/stream", (route) => {
    streamCalls += 1;
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
  const snapshot = {
    conversation_id: "fixture-conversation",
    version: 2,
    intent: { requested_count: 1, country: "RO" },
    result: {
      recommendations: [recommendation],
      requested_count: 1,
      shortfall_reason: null,
    },
    issues: [],
    provider_lookups: [
      { tmdb_id: 603, country: "RO", outcome: "ok", provider_count: 1, detail: null },
    ],
  };
  await page.route("**/api/recommendations/conversations/fixture-conversation/availability", async (route) => {
    availabilityRequests.push(route.request().postDataJSON());
    const updated = structuredClone(snapshot);
    updated.version = 3;
    updated.intent.country = "US";
    updated.result.recommendations[0].movie.watch_options = {
      country: "US",
      subscription: [{ provider_id: 9, name: "Max", logo_path: "/max.jpg" }],
      free: [],
      ads: [],
      rent: [],
      buy: [],
    };
    updated.provider_lookups = [
      { tmdb_id: 603, country: "US", outcome: "ok", provider_count: 1, detail: null },
    ];
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(updated) });
  });
  await page.route("**/api/recommendations/conversations/fixture-conversation", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(snapshot),
    }),
  );
  await page.goto("/");
  await page.getByLabel("Passcode").fill("fixture-passcode");
  await page.getByRole("button", { name: "Enter" }).click();
  await page.getByLabel("Message").fill("One science fiction movie");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".turnStatus").last()).toHaveText("Complete");
  await expect(page.getByRole("heading", { name: "The Matrix" })).toBeVisible();
  await expect(page.locator(".movieRating")).toContainText("8.2 / 10");
  await expect(page.locator(".providerBadge img")).toHaveAttribute(
    "src",
    "https://image.tmdb.org/t/p/w92/netflix.jpg",
  );
  await expect(page.locator(".activityMessage")).toHaveCount(1);
  const availabilityCountry = page.getByLabel("Streaming country");
  await availabilityCountry.focus();
  await availabilityCountry.selectOption("US");
  await expect(page.getByLabel("Streaming country")).toBeFocused();
  await expect(page.locator(".providers")).toContainText("Max");
  await expect(page.locator(".chatTranscript .activityMessage").last()).toHaveText(
    "Streaming availability updated for United States.",
  );
  expect(availabilityRequests).toEqual([{ country: "US", expected_version: 2 }]);
  expect(streamCalls).toBe(1);
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem("movie-agent-conversation")))
    .toBeNull();
});

test("shows streamed failures without restoring or persisting a conversation pointer", async ({ page }) => {
  const savedConversation = { id: "fixture-conversation", version: 3 };
  await page.addInitScript((conversation) => {
    window.sessionStorage.setItem("movie-agent-conversation", JSON.stringify(conversation));
    window.sessionStorage.setItem("movie-agent-thread:fixture-conversation", "old private text");
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
  await page.getByLabel("Message").fill("three comedies");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".turnStatus").last()).toHaveText("Failed");
  await expect(page.locator(".agentError")).toBeFocused();
  expect(await page.evaluate(() => sessionStorage.getItem("movie-agent-conversation"))).toBeNull();
  expect(await page.evaluate(() => sessionStorage.getItem("movie-agent-thread:fixture-conversation"))).toBeNull();
  await expect(page.locator(".agentReply [role=alert]")).toContainText("couldn’t interpret that request");
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem("movie-agent-conversation")))
    .toBeNull();
});

test("shows a clarification prompt as an announced result state", async ({ page }) => {
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
    const event = (
      sequence: number,
      type: string,
      stage: string,
      summary: string,
      data?: Record<string, unknown>,
    ) =>
      `id: ${sequence}\nevent: ${type}\ndata: ${JSON.stringify({
        event_id: `clarification-event-${sequence}`,
        run_id: "fixture-clarification-run",
        sequence,
        stage,
        type,
        origin: "application",
        summary,
        timestamp: "2026-10-25T12:00:00Z",
        metadata: {},
        data,
      })}\n\n`;
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: {
        "x-conversation-id": "fixture-clarification",
        "x-run-id": "fixture-clarification-run",
      },
      body: [
        event(1, "run_started", "predict", "Starting a protected movie search"),
        event(2, "needs_clarification", "complete", "Which decade do you prefer?", {
          intent: { requested_count: 5, country: "RO" },
          recommendations: [],
          requested_count: 5,
          shortfall_reason: null,
          clarification: "Which decade do you prefer?",
          issues: [],
          provider_lookups: [],
        }),
      ].join(""),
    });
  });
  await page.route("**/api/recommendations/conversations/fixture-clarification", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        conversation_id: "fixture-clarification",
        version: 2,
        intent: { requested_count: 5, country: "RO" },
        result: null,
        clarification: "Which decade do you prefer?",
        issues: [],
        provider_lookups: [],
      }),
    }),
  );

  await page.goto("/");
  await page.getByLabel("Passcode").fill("fixture-passcode");
  await page.getByRole("button", { name: "Enter" }).click();
  await page
    .getByLabel("Message")
    .fill("Help me narrow this down");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".turnStatus").last()).toHaveText("Needs clarification");
  const clarification = page.locator(".agentText");
  await expect(clarification.locator("p").first()).toHaveText("Which decade do you prefer?");
  await expect(clarification).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem("movie-agent-conversation")))
    .toBeNull();
});

test("starts a blank conversation on reload while keeping a valid signed session", async ({ page }) => {
  let conversationReads = 0;
  await page.addInitScript(() => {
    window.sessionStorage.setItem("movie-agent-entry-unlocked", "true");
    window.sessionStorage.setItem(
      "movie-agent-conversation",
      JSON.stringify({ id: "fixture-clarification", version: 2 }),
    );
  });
  await page.route("**/api/auth/session", (route) =>
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
  await page.route("**/api/recommendations/conversations/**", (route) => {
    conversationReads += 1;
    return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not found"}' });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "What should we watch tonight?" })).toBeVisible();
  await expect(page.getByLabel("Message")).toHaveValue("");
  expect(conversationReads).toBe(0);
  expect(await page.evaluate(() => sessionStorage.getItem("movie-agent-conversation"))).toBeNull();
  expect(await page.evaluate(() => sessionStorage.getItem("movie-agent-entry-unlocked"))).toBeNull();
  await page.reload();
  await expect(page.getByRole("heading", { name: "What should we watch tonight?" })).toBeVisible();
  expect(conversationReads).toBe(0);
});

test("an expired signed session asks for the passcode and starts blank", async ({ page }) => {
  let conversationReads = 0;
  await page.addInitScript(() => {
    sessionStorage.setItem("movie-agent-conversation", JSON.stringify({ id: "old-conversation", version: 3 }));
    sessionStorage.setItem("movie-agent-thread:old-conversation", "old private text");
  });
  await page.route("**/api/auth/session", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: '{"detail":"locked"}' }),
  );
  await page.route("**/api/auth/unlock", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, session_id: "fixture-session", expires_at: "2030-01-01T00:00:00Z" }),
    }),
  );
  await page.route("**/api/recommendations/conversations/**", (route) => {
    conversationReads += 1;
    return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not found"}' });
  });

  await page.goto("/");
  await expect(page.getByLabel("Passcode")).toBeVisible();
  await page.getByLabel("Passcode").fill("fixture-passcode");
  await page.getByRole("button", { name: "Enter" }).click();
  await expect(page.getByRole("heading", { name: "What should we watch tonight?" })).toBeVisible();
  expect(conversationReads).toBe(0);
  expect(await page.evaluate(() => sessionStorage.getItem("movie-agent-conversation"))).toBeNull();
  expect(await page.evaluate(() => sessionStorage.getItem("movie-agent-thread:old-conversation"))).toBeNull();
});

test("a valid signed session survives reloads while each entry starts a new conversation", async ({ page }) => {
  let sessionChecks = 0;
  let unlocks = 0;
  let authenticated = false;
  let conversationReads = 0;
  const context = page.context();
  await context.route("**/api/auth/session", (route) => {
    sessionChecks += 1;
    return route.fulfill(authenticated
      ? { status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true, session_id: "fixture-session", expires_at: "2030-01-01T00:00:00Z" }) }
      : { status: 401, contentType: "application/json", body: '{"detail":"locked"}' });
  });
  await context.route("**/api/auth/unlock", (route) => {
    unlocks += 1;
    authenticated = true;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true, session_id: "fixture-session", expires_at: "2030-01-01T00:00:00Z" }),
    });
  });
  await context.route("**/api/recommendations/conversations/**", (route) => {
    conversationReads += 1;
    return route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not found"}' });
  });

  await page.goto("/");
  await expect(page.getByLabel("Passcode")).toBeVisible();
  expect(sessionChecks).toBeGreaterThan(0);
  const firstEntryChecks = sessionChecks;
  await page.getByLabel("Passcode").fill("fixture-passcode");
  await page.getByRole("button", { name: "Enter" }).click();
  await expect(page.getByRole("heading", { name: "What should we watch tonight?" })).toBeVisible();
  expect(unlocks).toBe(1);

  await page.evaluate(() => {
    sessionStorage.setItem("movie-agent-entry-unlocked", "true");
    sessionStorage.setItem("movie-agent-conversation", JSON.stringify({ id: "fixture-conversation", version: 3 }));
    sessionStorage.setItem("movie-agent-thread:fixture-conversation", "old private text");
  });
  await page.reload();
  await expect(page.getByRole("heading", { name: "What should we watch tonight?" })).toBeVisible();
  await expect(page.getByLabel("Message")).toHaveValue("");
  expect(sessionChecks).toBeGreaterThan(firstEntryChecks);
  const reloadChecks = sessionChecks;
  expect(conversationReads).toBe(0);
  expect(await page.evaluate(() => sessionStorage.getItem("movie-agent-conversation"))).toBeNull();

  const popupPromise = context.waitForEvent("page");
  await page.evaluate(() => window.open("/", "_blank"));
  const newTab = await popupPromise;
  await newTab.waitForLoadState();
  await expect(newTab.getByRole("heading", { name: "What should we watch tonight?" })).toBeVisible();
  await expect(newTab.getByLabel("Passcode")).toHaveCount(0);
  expect(sessionChecks).toBeGreaterThan(reloadChecks);
  expect(conversationReads).toBe(0);
  expect(unlocks).toBe(1);
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
