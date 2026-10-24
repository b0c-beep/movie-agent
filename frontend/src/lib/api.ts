import { SseParser } from "@/lib/sse";

export type TraceEvent = {
  event_id: string;
  run_id: string;
  sequence: number;
  stage: "predict" | "decide" | "act" | "complete";
  type: string;
  origin: "agent" | "application";
  summary: string;
  timestamp: string;
  metadata: Record<string, string | number | boolean | null>;
};

export async function streamFixture(
  prompt: string,
  onEvent: (event: TraceEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch("/api/fixture-stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ prompt }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Fixture stream failed with status ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();

  const consume = (text: string) => {
    for (const message of parser.feed(text)) {
      onEvent(JSON.parse(message.data) as TraceEvent);
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    consume(decoder.decode(value, { stream: true }));
  }

  consume(decoder.decode());
  for (const message of parser.finish()) {
    onEvent(JSON.parse(message.data) as TraceEvent);
  }
}
