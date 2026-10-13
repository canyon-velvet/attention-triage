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
  headline: {
    actions: number;
    captured: number;
    need_review: number;
    suppressed: number; // hidden by "this and future ones" since the last review
    since: string | null;
  };
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
async function post(path: string, body: unknown = {}, method = "POST"): Promise<void> {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    // The server's reason, when it gives one in words (a refused pattern says why).
    const detail = await response.json().then((b) => b?.detail, () => undefined);
    throw new Error(`HTTP ${response.status}${typeof detail === "string" ? `: ${detail}` : ""}`);
  }
}

export type Pattern = { kind: "exact" | "prefix" | "directory"; value: string };

// "Fine → this and future ones". No pattern: the flag's exact target.
export const suppressFlag = (id: number, pattern: Pattern | null, note: string) =>
  post(`/api/flags/${id}/dismiss`, { scope: "future", pattern, note: note || null });

export const dismissFlag = (id: number) => post(`/api/flags/${id}/dismiss`, { scope: "once" });

export const acknowledgeFlag = (id: number) => post(`/api/flags/${id}/ack`);

// Only what this page shows: flags that arrived since stay open for the next review.
export const markAllReviewed = (digest: Digest) =>
  post("/api/review/mark-all", {
    flag_ids: digest.projects.flatMap((p) => p.sessions.flatMap((s) => s.flags.map((f) => f.id))),
    as_of: digest.as_of,
  });

// The shape of GET /api/suppressions (suppress.listing in src/attention_triage/suppress.py).
export type Suppression = {
  id: number;
  rule: string;
  kind: Pattern["kind"];
  pattern: string;
  project: string | null;
  note: string | null;
  created_at: string;
  hits: number; // flags it hid
  last_hit: string | null;
  suggestion: string | null; // a config change, once it hides many flags a week
};

export async function fetchSuppressions(): Promise<Suppression[]> {
  const response = await fetch("/api/suppressions");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

// Undo: later matches are flagged again, and the flags it hid reopen.
export const undoSuppression = (id: number) => post(`/api/suppressions/${id}`, {}, "DELETE");

// The shape of GET /api/sessions/{id} (src/attention_triage/session.py).
export type SessionEvent = {
  // tool_call, or session_start, turn_end, session_end, subagent_start, subagent_stop; or a
  // tool_result, tool_failure or permission_denied whose call wasn't stored
  event_type: string;
  tool: string | null;
  target_kind: string | null;
  target: string | null;
  stated_reason: string | null;
  project: string | null;
  time: string;
  outcome: Flag["outcome"] | null; // tool calls only
  flags: { id: number; rule: string; severity: Flag["severity"]; label: string; reason: string; status: string }[];
};

export type SessionDetail = {
  session_id: string;
  events: SessionEvent[]; // the main agent's
  subagents: { agent_id: string; agent_type: string | null; events: SessionEvent[] }[];
};

export async function fetchSession(id: string): Promise<SessionDetail> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(id)}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
