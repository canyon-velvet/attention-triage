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
  headline: { actions: number; captured: number; need_review: number };
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
