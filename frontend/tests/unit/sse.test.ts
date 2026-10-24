import { describe, expect, it } from "vitest";

import { SseParser } from "../../src/lib/sse";

describe("SseParser", () => {
  it("handles split chunks and multiline data", () => {
    const parser = new SseParser();

    expect(parser.feed("id: 1\nevent: dec")).toEqual([]);
    expect(parser.feed("ision\ndata: {\"part\":1}\ndata: tail\n\n")).toEqual([
      { id: "1", event: "decision", data: '{"part":1}\ntail' },
    ]);
  });

  it("normalizes CRLF frame boundaries", () => {
    const parser = new SseParser();
    expect(parser.feed("event: completed\r\ndata: {}\r\n\r\n")).toEqual([
      { event: "completed", data: "{}" },
    ]);
  });
});
