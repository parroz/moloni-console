// ── Wire types from backend ──

export type SupplierOption = { slug: string; label: string };

export type Usage = {
  input_tokens?: number;
  output_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
};

export type AgentSession = {
  session_id: string;
  supplier_slug: string;
  test_mode: boolean;
  pdf_filename: string | null;
  has_pdf: boolean;
  messages: AssistantOrUserMessage[];
  created_at: number;
};

export type AssistantOrUserMessage = {
  role: "user" | "assistant";
  content: ContentBlock[];
};

export type ContentBlock =
  | { type: "text"; text: string }
  | { type: "document"; source: unknown }
  | {
      type: "tool_use" | "mcp_tool_use";
      id: string;
      name: string;
      server_name?: string;
      input: Record<string, unknown>;
    }
  | {
      type: "mcp_tool_result";
      tool_use_id: string;
      is_error?: boolean;
      content: { type: string; text?: string }[];
    };

// ── SSE events emitted by the runner ──

export type AgentEvent =
  | { type: "user_message_persisted" }
  | { type: "stream_started" }
  | { type: "text_delta"; text: string }
  | {
      type: "tool_use";
      id: string;
      name: string;
      server_name: string;
      input: Record<string, unknown>;
    }
  | {
      type: "tool_result";
      tool_use_id: string;
      content: { type: string; text?: string }[];
      is_error: boolean;
    }
  | { type: "message_complete"; stop_reason: string | null; usage: Usage }
  | { type: "error"; message: string }
  | { type: "done" };
