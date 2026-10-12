// The shape of GET /api/digest (src/attention_triage/digest.py).

// src/attention_triage/outcome.py; only the failed kinds have error text.
export type OutcomeKind =
  | "ran"
  | "approved"
  | "failed"
  | "approved_failed"
  | "denied_by_auto_mode"
  | "denied_by_user"
  | "unknown";

export type Flag = {
  id: number;
  tool: string | null;
  target_kind: string | null;
  target: string | null;
  stated_reason: string | null;
  rule: string;
  severity: "high" | "review";
  label: string;
  evidence: Record<string, unknown>;
  time: string;
  project: string | null;
  outcome: { kind: OutcomeKind; error: string | null };
};

export type Digest = {
  as_of: string; // when the digest was built; "mark all reviewed" starts the next window there
  headline: { actions: number; captured: number; need_review: number; since: string | null };
  projects: {
    project: string | null;
    sessions: { session_id: string | null; flags: Flag[] }[];
  }[];
};

export async function fetchDigest(): Promise<Digest> {
  const response = await fetch("/api/digest");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

// Review actions (src/attention_triage/server.py). Same origin, so the server's CSRF check passes.
async function post(path: string, body: unknown = {}): Promise<void> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

export const dismissFlag = (id: number) => post(`/api/flags/${id}/dismiss`, { scope: "once" });

export const acknowledgeFlag = (id: number) => post(`/api/flags/${id}/ack`);

// Only what this page shows: flags that arrived since stay open for the next review.
export const markAllReviewed = (digest: Digest) =>
  post("/api/review/mark-all", {
    flag_ids: digest.projects.flatMap((p) => p.sessions.flatMap((s) => s.flags.map((f) => f.id))),
    as_of: digest.as_of,
  });
