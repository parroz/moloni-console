import { useCallback, useRef, useState } from "react";

/**
 * Generic hook for streaming SSE events from a POST endpoint.
 * EventSource doesn't support POST, so we use fetch + a manual SSE parser.
 */
export function useSSEPost<T = unknown>() {
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const stream = useCallback(async (url: string, onEvent: (e: T) => void) => {
    const ac = new AbortController();
    abortRef.current = ac;
    setStreaming(true);
    try {
      const res = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "text/event-stream" },
        signal: ac.signal,
      });
      if (!res.ok || !res.body) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(errText || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const dataLine = raw
            .split("\n")
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).trimStart())
            .join("\n");
          if (!dataLine) continue;
          try {
            onEvent(JSON.parse(dataLine) as T);
          } catch {
            // ignore malformed lines
          }
        }
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, []);

  const abort = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { stream, abort, streaming };
}
