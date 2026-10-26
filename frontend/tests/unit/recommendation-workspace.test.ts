import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RecommendationWorkspace } from "@/components/recommendation-workspace";

const mocks = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getRecommendationCapabilities: vi.fn(),
  getRunStatus: vi.fn(),
  streamRecommendations: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getConversation: mocks.getConversation,
  getRecommendationCapabilities: mocks.getRecommendationCapabilities,
  getRunStatus: mocks.getRunStatus,
  streamRecommendations: mocks.streamRecommendations,
}));

function traceEvent(type: string, overrides: Record<string, unknown> = {}) {
  return {
    event_id: `${type}-event`,
    run_id: "run-1",
    sequence: 1,
    stage: "complete",
    type,
    origin: "application",
    summary: `${type} summary`,
    timestamp: "2026-10-25T12:00:00Z",
    metadata: {},
    data: null,
    ...overrides,
  };
}

async function submitPrompt(prompt: string) {
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: prompt } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("RecommendationWorkspace terminal events", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    mocks.getConversation.mockReset();
    mocks.getConversation.mockResolvedValue(null);
    mocks.getRecommendationCapabilities.mockReset();
    mocks.getRecommendationCapabilities.mockResolvedValue(null);
    mocks.getRunStatus.mockReset();
    mocks.streamRecommendations.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows a safe failure without persisting conversation state", async () => {
    mocks.streamRecommendations.mockImplementation(
      async (_input: unknown, onEvent: (event: unknown) => void) => {
        onEvent(
          traceEvent("failed", {
            metadata: {
              error_code: "recommendation_failed",
              failure_stage: "intent_extraction",
              exception_type: "UnexpectedModelBehavior",
            },
          }),
        );
        return { conversationId: "conv-1", runId: "run-1" };
      },
    );

    render(createElement(RecommendationWorkspace));
    await submitPrompt("three comedies");

    expect(await screen.findByRole("alert")).toHaveTextContent("couldn’t interpret that request");
    expect(screen.getByRole("alert")).toHaveTextContent("couldn’t interpret that request");
    expect(window.sessionStorage.getItem("movie-agent-conversation")).toBeNull();
    expect(mocks.streamRecommendations).toHaveBeenCalledWith(
      expect.objectContaining({ conversationId: null, expectedVersion: null }),
      expect.any(Function),
      expect.any(AbortSignal),
      expect.any(Function),
    );
  });

  it("still advances the conversation when the run completes", async () => {
    mocks.getConversation.mockResolvedValue({
      conversation_id: "conv-2",
      version: 2,
      intent: {},
      result: null,
    });
    mocks.streamRecommendations.mockImplementation(
      async (_input: unknown, onEvent: (event: unknown) => void) => {
        onEvent(traceEvent("recommendations", { data: { recommendations: [] } }));
        onEvent(traceEvent("completed"));
        return { conversationId: "conv-2", runId: "run-2" };
      },
    );

    render(createElement(RecommendationWorkspace));
    await submitPrompt("three comedies");

    expect(await screen.findByText("Complete")).toBeInTheDocument();
    expect(window.sessionStorage.getItem("movie-agent-conversation")).toBeNull();
  });
});
