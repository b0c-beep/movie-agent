"use client";

import { FormEvent, useRef, useState } from "react";

import { streamFixture, type TraceEvent } from "@/lib/api";

export function FixtureDemo() {
  const [prompt, setPrompt] = useState("A dark romance from the 90s");
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState("Ready");
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setEvents([]);
    setError(null);
    setStatus("Streaming");

    try {
      await streamFixture(
        prompt,
        (nextEvent) => {
          setEvents((current) => [...current, nextEvent]);
          setStatus(nextEvent.type === "completed" ? "Complete" : nextEvent.stage);
        },
        controller.signal,
      );
    } catch (reason) {
      if (controller.signal.aborted) {
        setStatus("Cancelled");
      } else {
        setStatus("Failed");
        setError(reason instanceof Error ? reason.message : "Unexpected stream failure");
      }
    }
  }

  return (
    <main>
      <section className="hero">
        <p className="eyebrow">Movie Agent · transport proof</p>
        <h1>What should we watch tonight?</h1>
        <p className="lede">
          This foundation streams deterministic predict → decide → act events. Real movie
          discovery arrives in the next phase.
        </p>

        <form onSubmit={handleSubmit}>
          <label htmlFor="prompt">Describe tonight&apos;s movie</label>
          <div className="promptRow">
            <input
              id="prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              minLength={1}
              maxLength={500}
              required
            />
            <button type="submit">Run fixture</button>
          </div>
        </form>
      </section>

      <section className="trace" aria-live="polite" aria-busy={status === "Streaming"}>
        <div className="traceHeader">
          <h2>Execution trace</h2>
          <span data-testid="status">{status}</span>
        </div>
        {error ? <p role="alert">{error}</p> : null}
        {events.length === 0 ? (
          <p className="empty">Run the fixture to verify incremental events.</p>
        ) : (
          <ol>
            {events.map((event) => (
              <li key={event.event_id}>
                <span>{event.stage}</span>
                <p>{event.summary}</p>
              </li>
            ))}
          </ol>
        )}
      </section>
    </main>
  );
}
