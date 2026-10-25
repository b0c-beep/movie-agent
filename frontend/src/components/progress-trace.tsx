"use client";

import { useEffect, useRef, useState } from "react";

import type { TraceEvent } from "@/lib/api";

const EVENT_TYPE_LABELS: Record<string, string> = {
  run_started: "Run started",
  intent_updated: "Preferences updated",
  decision: "Decision",
  tool_started: "Tool started",
  tool_finished: "Tool finished",
  recommendations: "Recommendations",
  completed: "Completed",
  needs_clarification: "Needs clarification",
  failed: "Run failed",
  cancelled: "Run cancelled",
};

export function eventTypeLabel(type: string): string {
  return (
    EVENT_TYPE_LABELS[type] ??
    type.replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase())
  );
}

export function ProgressTrace({
  events,
  status,
  isRunning,
  error,
}: {
  events: TraceEvent[];
  status: string;
  isRunning: boolean;
  error: string | null;
}) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const latest = events.length ? events[events.length - 1] : null;

  useEffect(() => {
    const node = scrollerRef.current;
    if (!node) return;
    if (atBottomRef.current) node.scrollTop = node.scrollHeight;
  }, [events.length]);

  function handleScroll() {
    const node = scrollerRef.current;
    if (!node) return;
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
    atBottomRef.current = atBottom;
    setIsAtBottom(atBottom);
  }

  function jumpToLatest() {
    const node = scrollerRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
    atBottomRef.current = true;
    setIsAtBottom(true);
  }

  const runOrder = new Map<string, number>();
  events.forEach((event) => {
    if (!runOrder.has(event.run_id)) runOrder.set(event.run_id, runOrder.size + 1);
  });

  return (
    <section className="trace" aria-labelledby="trace-heading">
      <div className="traceHeader">
        <h2 id="trace-heading">Search progress</h2>
        <span aria-live="polite" data-testid="status" role="status">
          {isRunning ? `${status}…` : status}
        </span>
      </div>
      <p className="srOnly" aria-atomic="true" aria-live="polite">
        {latest ? `${eventTypeLabel(latest.type)}: ${latest.summary}` : ""}
      </p>
      {error ? <p role="alert">{error}</p> : null}
      {events.length === 0 ? (
        <p className="empty">Your search steps will appear here.</p>
      ) : (
        <>
          <div
            aria-label="Search progress events, newest at the bottom"
            className="traceScroller"
            onScroll={handleScroll}
            ref={scrollerRef}
            role="region"
            tabIndex={0}
          >
            <ol>
              {events.map((event, index) => {
                const firstOfRun =
                  index === 0 || events[index - 1].run_id !== event.run_id;
                return (
                  <li key={event.event_id}>
                    <div className="traceLabels">
                      {firstOfRun ? (
                        <span className="traceRun">Turn {runOrder.get(event.run_id) ?? 1}</span>
                      ) : null}
                      <span className="traceStage">{event.stage}</span>
                      <span className="traceOrigin" data-origin={event.origin}>
                        {event.origin === "agent" ? "Agent" : "Application"}
                      </span>
                      <span className="traceType">{eventTypeLabel(event.type)}</span>
                    </div>
                    <p>{event.summary}</p>
                  </li>
                );
              })}
            </ol>
          </div>
          {!isAtBottom ? (
            <button className="quietButton traceJump" onClick={jumpToLatest} type="button">
              Jump to newest progress
            </button>
          ) : null}
        </>
      )}
    </section>
  );
}
