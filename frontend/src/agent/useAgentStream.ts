import { useCallback, useRef, useState } from "react";
import type { AgentEvent } from "./types";

/**
 * Streams agent SSE events from POST /api/agent/sessions/:id/messages.
 * EventSource doesn't support POST, so we use fetch + a manual SSE parser.
 */
export function useAgentStream(sessionId: string | null) {
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const send = useCallback(
    async (text: string, onEvent: (e: AgentEvent) => void) => {
      if (!sessionId) throw new Error("No session");
      const ac = new AbortController();
      abortRef.current = ac;
      setStreaming(true);
      try {
        const res = await fetch(`/api/agent/sessions/${sessionId}/messages`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
          body: JSON.stringify({ text }),
          signal: ac.signal,
        });
        if (!res.ok || !res.body) {
          const errText = await res.text().catch(() => res.statusText);
          throw new Error(errText || `HTTP ${res.status}`);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        // SSE messages are separated by a blank line. We split on \n\n.
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
              const payload = JSON.parse(dataLine) as AgentEvent;
              onEvent(payload);
            } catch {
              // ignore malformed lines
            }
          }
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [sessionId],
  );

  const abort = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { send, abort, streaming };
}
