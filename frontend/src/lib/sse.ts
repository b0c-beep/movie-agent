export type SseMessage = {
  id?: string;
  event?: string;
  data: string;
};

export class SseParser {
  private buffer = "";

  feed(chunk: string): SseMessage[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const messages: SseMessage[] = [];
    let boundary = this.buffer.indexOf("\n\n");

    while (boundary >= 0) {
      const frame = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const parsed = this.parseFrame(frame);
      if (parsed) messages.push(parsed);
      boundary = this.buffer.indexOf("\n\n");
    }

    return messages;
  }

  finish(): SseMessage[] {
    const trailing = this.buffer.trim();
    this.buffer = "";
    const parsed = trailing ? this.parseFrame(trailing) : null;
    return parsed ? [parsed] : [];
  }

  private parseFrame(frame: string): SseMessage | null {
    const message: SseMessage = { data: "" };
    const dataLines: string[] = [];

    for (const line of frame.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const separator = line.indexOf(":");
      const field = separator >= 0 ? line.slice(0, separator) : line;
      const rawValue = separator >= 0 ? line.slice(separator + 1) : "";
      const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;

      if (field === "id") message.id = value;
      if (field === "event") message.event = value;
      if (field === "data") dataLines.push(value);
    }

    if (dataLines.length === 0) return null;
    message.data = dataLines.join("\n");
    return message;
  }
}
