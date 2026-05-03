// ── Extracted invoice (matches backend schema.py) ──────────────────────────────

export type SupplierOption = { slug: string; label: string };

export type ExtractedSupplier = {
  slug: string;
  name: string;
  vat: string;
  moloni_supplier_id: number;
};

export type ExtractedHeader = {
  invoice_number: string;
  date: string;
  expiration_date: string | null;
  currency: string;
  subtotal: number;
  tax_total: number;
  grand_total: number;
};

export type ExtractedLine = {
  reference: string;
  name: string;
  summary: string;
  qty: number;
  unit_cost: number;
  pvp_with_vat: number;
  moloni_price_no_vat: number;
  subcategory_name: string;
  color: string;
  size: string;
};

export type Reconciliation = {
  calculated_subtotal: number;
  matches_invoice_total: boolean;
  warnings: string[];
};

export type ExtractedInvoice = {
  supplier: ExtractedSupplier;
  header: ExtractedHeader;
  lines: ExtractedLine[];
  reconciliation: Reconciliation;
};

// ── Session (matches backend _public_session) ───────────────────────────────────

export type AgentSession = {
  session_id: string;
  supplier_slug: string;
  pdf_filename: string | null;
  has_pdf: boolean;
  extracted: ExtractedInvoice | null;
  apply_log: ApplyEvent[];
  created_at: number;
};

// ── SSE events emitted by /apply ────────────────────────────────────────────────

export type ApplyEvent =
  | { type: "started" }
  | { type: "subcategory_lookup"; needs_creation: string[] }
  | { type: "subcategory_created"; name: string; category_id: number }
  | { type: "line_matched"; reference: string; product_id: number }
  | { type: "line_creating"; reference: string }
  | { type: "line_created"; reference: string; product_id: number }
  | { type: "line_error"; reference: string; message: string }
  | { type: "invoice_creating" }
  | { type: "invoice_created"; document_id: number }
  | { type: "invoice_error"; message: string }
  | { type: "done" };
