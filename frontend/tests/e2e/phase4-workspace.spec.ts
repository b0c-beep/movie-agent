import { expect, test, type Page, type Route } from "@playwright/test";

const SESSION = {
  authenticated: true,
  session_id: "fixture-session",
  expires_at: "2030-01-01T00:00:00Z",
};

function recommendation(title: string, tmdbId: number, year: number) {
  return {
    rank: 1,
    match_reason: "TMDB lists this movie with the requested facts.",
    caveats: [],
    movie: {
      tmdb_id: tmdbId,
      title,
      release_year: year,
      runtime_minutes: 120,
      genres: ["Comedy"],
      overview: "A grounded movie description.",
      poster_url: null,
      tmdb_url: `https://www.themoviedb.org/movie/${tmdbId}`,
      imdb_url: null,
      ratings: [{ source: "tmdb", value: 8.1, scale: "10", vote_count: 200 }],
      watch_options: {
        country: "RO",
        subscription: [],
        free: [],
        ads: [],
        rent: [],
        buy: [],
      },
    },
  };
}

function recommendationResult(overrides: Record<string, unknown> = {}) {
  return {
    intent: { genres: ["Comedy"], requested_count: 1, country: "RO" },
    recommendations: [recommendation("The Truman Show", 37165, 1998)],
    requested_count: 1,
    shortfall_reason: null,
    issues: [],
    provider_lookups: [],
    ...overrides,
  };
}

function traceEvent(
  runId: string,
  sequence: number,
  type: string,
  stage: string,
  summary: string,
  data?: Record<string, unknown>,
  metadata: Record<string, unknown> = {},
) {
  return {
    event_id: `${runId}-${sequence}`,
    run_id: runId,
    sequence,
    stage,
    type,
    origin: "application",
    summary,
    timestamp: "2026-10-25T12:00:00Z",
    metadata,
    data: data ?? null,
  };
}

async function authenticate(page: Page) {
  await page.route("**/api/auth/session", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SESSION) }),
  );
}

async function capabilities(page: Page, supported: boolean) {
  await page.route("**/api/recommendations/capabilities", (route) =>
    supported
      ? route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            history: true,
            structured_clarification: true,
            filter_edit: true,
            alternatives: true,
            run_status: true,
          }),
        })
      : route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Not found" }),
        }),
  );
}

function routeJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function sseResponse(
  route: Route,
  conversationId: string,
  runId: string,
  events: Record<string, unknown>[],
) {
  return route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: {
      "x-conversation-id": conversationId,
      "x-run-id": runId,
    },
    body: events
      .map(
        (event) =>
          `id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`,
      )
      .join(""),
  });
}

test("starts blank on page load without restoring a saved conversation", async ({ page }) => {
  let conversationReads = 0;
  await authenticate(page);
  await capabilities(page, true);
  await page.addInitScript(() => {
    sessionStorage.setItem("movie-agent-conversation", JSON.stringify({ id: "old", version: 4 }));
    sessionStorage.setItem("movie-agent-thread:old", "private old conversation text");
  });
  await page.route("**/api/recommendations/conversations/**", (route) => {
    conversationReads += 1;
    return routeJson(route, {} , 404);
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "What should we watch tonight?" })).toBeVisible();
  await expect(page.getByLabel("Message")).toHaveValue("");
  await expect(page.getByText("private old conversation text")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "New chat" })).toHaveCount(0);
  await expect(page.locator(".examplePrompt")).toHaveCount(7);
  await page.getByLabel("Message").focus();
  await page.getByLabel("Message").press("ArrowDown");
  await expect(page.getByRole("option", { name: "Show me three action movies." })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.getByLabel("Message").press("Enter");
  await expect(page.getByLabel("Message")).toHaveValue("Show me three action movies.");
  await expect(page.getByLabel("Message")).toBeFocused();
  await page.getByLabel("Message").press("Escape");
  await expect(page.locator(".examplePrompts ul")).toHaveCount(0);
  await expect(page.getByLabel("Chat tools")).toBeVisible();
  expect(conversationReads).toBe(0);
  for (const width of [1440, 1024, 768, 760, 390, 320]) {
    await page.setViewportSize({ width, height: 760 });
    const countryBox = await page.getByLabel("Streaming country").boundingBox();
    expect(countryBox!.x).toBeGreaterThanOrEqual(0);
    expect(countryBox!.x + countryBox!.width).toBeLessThanOrEqual(width);
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
      .toBeLessThanOrEqual(width);
    if (width > 1024) {
      const transcriptBox = await page.getByLabel("Conversation").boundingBox();
      expect(countryBox!.x).toBeLessThan(transcriptBox!.x);
    }
    if (width <= 760) {
      const toolsBox = await page.getByLabel("Chat tools").boundingBox();
      const availabilityBox = await page.getByLabel("Availability settings").boundingBox();
      const composerBox = await page.locator(".chatComposerArea").boundingBox();
      expect(toolsBox!.y).toBeLessThan(availabilityBox!.y);
      expect(availabilityBox!.y).toBeLessThan(composerBox!.y);
    }
  }
});

test("Cancel stops the active chat request", async ({ page }) => {
  await authenticate(page);
  await capabilities(page, true);
  let releaseRequest!: () => void;
  let markStarted!: () => void;
  const heldRequest = new Promise<void>((resolve) => {
    releaseRequest = resolve;
  });
  const requestStarted = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  await page.route("**/api/recommendations/stream", async (route) => {
    markStarted();
    await heldRequest;
    try {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    } catch {
      // The request was intentionally aborted by the Cancel control.
    }
  });

  await page.goto("/");
  await page.getByLabel("Message").fill("Find me a comedy");
  await page.getByRole("button", { name: "Send" }).click();
  await requestStarted;
  await expect(page.getByLabel("Message")).toHaveValue("");
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Cancel" })).toHaveCount(0);
  await expect(page.getByLabel("Message")).toHaveValue("Find me a comedy");
  releaseRequest();
});

test("keeps chat scroll position while reading older activity", async ({
  page,
}) => {
  const runEvents = (runId: string, label: string) => [
    traceEvent(runId, 1, "run_started", "predict", `${label}: starting`),
    traceEvent(runId, 2, "decision", "decide", `${label}: interpreting preferences`),
    traceEvent(runId, 3, "tool_started", "act", `${label}: checking TMDB`),
    traceEvent(runId, 4, "tool_finished", "act", `${label}: TMDB verification finished`),
  ];
  const snapshot = {
    conversation_id: "fixture-trace",
    version: 4,
    intent: { requested_count: 1, country: "RO" },
    result: recommendationResult(),
    issues: [],
    provider_lookups: [],
    turns: [
      {
        run_id: "run-trace-1",
        user_message: "first request",
        status: "completed",
        response: recommendationResult(),
        answer: null,
        events: runEvents("run-trace-1", "first turn"),
      },
      {
        run_id: "run-trace-2",
        user_message: "second request",
        status: "completed",
        response: recommendationResult(),
        answer: null,
        events: runEvents("run-trace-2", "second turn"),
      },
      {
        run_id: "run-trace-3",
        user_message: "third request",
        status: "completed",
        response: recommendationResult(),
        answer: null,
        events: runEvents("run-trace-3", "third turn"),
      },
    ],
  };
  await authenticate(page);
  await capabilities(page, true);
  await page.route("**/api/recommendations/conversations/fixture-trace", (route) =>
    routeJson(route, snapshot),
  );
  await page.route("**/api/recommendations/stream", (route) => {
    const runId = "run-trace-4";
    const events = Array.from({ length: 24 }, (_, index) =>
      traceEvent(runId, index + 1, index === 0 ? "run_started" : "tool_finished", index === 0 ? "predict" : "act", `Search step ${index + 1}`),
    );
    events.push(
      traceEvent(runId, 25, "recommendations", "complete", "fourth turn: picks ready", recommendationResult()),
      traceEvent(runId, 26, "completed", "complete", "fourth turn: complete"),
    );
    return sseResponse(route, "fixture-trace", runId, events);
  });

  await page.goto("/");
  await page.getByLabel("Message").fill("Show me one comedy");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".turnStatus").last()).toHaveText("Complete");
  const scroller = page.locator(".chatTranscript");
  await expect(scroller).toBeVisible();
  await expect
    .poll(() =>
      scroller.evaluate(
        (node) => node.scrollHeight > node.clientHeight && node.scrollTop > 0,
      ),
    )
    .toBe(true);

  await scroller.evaluate((node) => {
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await expect(page.getByRole("button", { name: "Jump to latest" })).toBeVisible();
  const before = await scroller.evaluate((node) => node.scrollTop);

  await page.getByLabel("Message").fill("newer comedies");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".turnStatus").last()).toHaveText("Complete");

  await expect
    .poll(() => scroller.evaluate((node) => node.scrollTop))
    .toBe(before);

  await page.getByRole("button", { name: "Jump to latest" }).click();
  await expect
    .poll(() => scroller.evaluate((node) => node.scrollTop > 0))
    .toBe(true);
});

test("sends structured clarification choices and filter edits when the backend supports them", async ({
  page,
}) => {
  const snapshot = {
    conversation_id: "fixture-support",
    version: 2,
    intent: { requested_count: 1, country: "RO" },
    result: null,
    clarification: "Which Jim Carrey do you mean?",
    clarification_options: [
      { choice_id: "person-1", kind: "person", label: "Jim Carrey", detail: "Actor" },
      { choice_id: "person-2", kind: "person", label: "Jim Carrey", detail: "Producer" },
    ],
    issues: [],
    provider_lookups: [],
  };
  await authenticate(page);
  await capabilities(page, true);
  await page.route("**/api/recommendations/conversations/fixture-support", (route) =>
    routeJson(route, snapshot),
  );
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/recommendations/stream", (route) => {
    requests.push(route.request().postDataJSON() as Record<string, unknown>);
    const runId = `run-support-${requests.length}`;
    const events = requests.length === 1
      ? [
          traceEvent(runId, 1, "run_started", "predict", "Starting a search"),
          traceEvent(runId, 2, "needs_clarification", "complete", "Which Jim Carrey do you mean?", snapshot),
        ]
      : [
      traceEvent(runId, 1, "run_started", "predict", "Starting a search"),
      traceEvent(
        runId,
        2,
        "recommendations",
        "complete",
        "Prepared one pick",
        recommendationResult({
          alternatives: [
            {
              recommendation: recommendation("Closest comedy", 999001, 2001),
              unmet_filters: ["Genre: Comedy"],
            },
          ],
        }),
      ),
      traceEvent(runId, 3, "completed", "complete", "Run completed"),
      ];
    return sseResponse(route, "fixture-support", runId, events);
  });

  await page.goto("/");
  await page.getByLabel("Message").fill("Find Jim Carrey movies");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("button", { name: "Use this choice" })).toBeVisible();
  await expect(page.locator(".clarificationChoices")).toBeFocused();
  await page.getByRole("button", { name: "Use this choice" }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1].clarification_choice_id).toBe("person-1");
  expect(String(requests[1].message)).toContain("I meant Jim Carrey");

  await expect(
    page.getByRole("button", { name: "Show details for The Truman Show" }),
  ).toBeVisible();
  await expect(page.locator(".activeFilters")).toContainText("Use country selector");
  await expect(page.locator(".activeFilters")).not.toContainText("Managed above");
  await page.getByRole("button", { name: "Edit Results requested filter" }).click();
  const editor = page.locator(".filterEditForm");
  const editorBox = await editor.boundingBox();
  const filterRailBox = await page.locator(".activeFilters").boundingBox();
  expect(editorBox!.x).toBeGreaterThanOrEqual(filterRailBox!.x);
  expect(editorBox!.x + editorBox!.width).toBeLessThanOrEqual(
    filterRailBox!.x + filterRailBox!.width,
  );
  await page.getByRole("button", { name: "Cancel edit" }).click();
  await expect(editor).toHaveCount(0);
  await page.getByRole("button", { name: "Edit Results requested filter" }).click();
  await page.getByLabel("New results requested").fill("2");
  await page.getByRole("button", { name: "Save filter" }).click();
  await expect.poll(() => requests.length).toBe(3);
  expect(requests[2].filter_edit).toEqual({
    field: "requested_count",
    operation: "set",
    value: 2,
  });
  await page.getByRole("button", { name: "Remove Genre Comedy filter" }).click();
  await expect.poll(() => requests.length).toBe(4);
  expect(requests[3].filter_edit).toEqual({
    field: "genres",
    operation: "remove",
    value: "Comedy",
  });
  const latestTurn = page.locator(".chatTurn").last();
  await expect(latestTurn.getByRole("heading", { name: "Closest alternatives" })).toBeVisible();
  await latestTurn.getByRole("button", { name: "Remove filter: Genre: Comedy" }).click();
  await expect.poll(() => requests.length).toBe(5);
  expect(requests[4].filter_edit).toEqual({
    field: "genres",
    operation: "remove",
    value: "Comedy",
  });
});

test("falls back to descriptive messages when the current backend lacks Phase 4 capabilities", async ({
  page,
}) => {
  const snapshot = {
    conversation_id: "fixture-fallback",
    version: 2,
    intent: { genres: ["Comedy"], requested_count: 1, country: "RO" },
    result: recommendationResult(),
    clarification: "Which Jim Carrey do you mean?",
    clarification_options: [
      { choice_id: "person-1", kind: "person", label: "Jim Carrey", detail: "Actor" },
      { choice_id: "person-2", kind: "person", label: "Jim Carrey", detail: "Producer" },
    ],
    issues: [],
    provider_lookups: [],
  };
  await authenticate(page);
  await capabilities(page, false);
  await page.route("**/api/recommendations/conversations/fixture-fallback", (route) =>
    routeJson(route, snapshot),
  );
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/recommendations/stream", (route) => {
    requests.push(route.request().postDataJSON() as Record<string, unknown>);
    const runId = `run-fallback-${requests.length}`;
    const events = requests.length === 1
      ? [
          traceEvent(runId, 1, "run_started", "predict", "Starting a search"),
          traceEvent(runId, 2, "needs_clarification", "complete", "Which Jim Carrey do you mean?", snapshot),
        ]
      : [
      traceEvent(runId, 1, "run_started", "predict", "Starting a search"),
      traceEvent(runId, 2, "recommendations", "complete", "Prepared one pick", recommendationResult()),
      traceEvent(runId, 3, "completed", "complete", "Run completed"),
      ];
    return sseResponse(route, "fixture-fallback", runId, events);
  });

  await page.goto("/");
  await page.getByLabel("Message").fill("Find Jim Carrey comedies");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("button", { name: "Use this choice" })).toBeVisible();
  await page.getByRole("button", { name: "Use this choice" }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1]).not.toHaveProperty("clarification_choice_id");
  expect(String(requests[1].message)).toContain("I meant Jim Carrey");

  await expect(
    page.getByRole("button", { name: "Show details for The Truman Show" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Remove Genre Comedy filter" }).click();
  await expect.poll(() => requests.length).toBe(3);
  expect(requests[2]).not.toHaveProperty("filter_edit");
  expect(String(requests[2].message)).toContain("Remove Comedy from the genre filter");
});

test("shows a rate-limit message with retry guidance and does not resubmit", async ({ page }) => {
  await authenticate(page);
  await capabilities(page, true);
  let streamCalls = 0;
  await page.route("**/api/recommendations/stream", (route) => {
    streamCalls += 1;
    const runId = "run-rate-limited";
    return sseResponse(route, "fixture-rate-limit", runId, [
      traceEvent(runId, 1, "run_started", "predict", "Starting a search"),
      traceEvent(
        runId,
        2,
        "failed",
        "complete",
        "The recommendation run could not be completed",
        undefined,
        { error_code: "model_rate_limited", retry_after_seconds: 15 },
      ),
    ]);
  });

  await page.goto("/");
  await page.getByLabel("Message").fill("three comedies");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".turnStatus").last()).toHaveText("Failed");
  await expect(page.locator(".agentReply [role=alert]")).toContainText(
    "The recommendation model is temporarily rate limited",
  );
  await expect(page.locator(".agentReply [role=alert]")).toContainText("Wait about 15 seconds");
  expect(streamCalls).toBe(1);
});
