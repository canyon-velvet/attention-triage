import type { Flag, OutcomeKind } from "./api";

const OUTCOMES: Record<OutcomeKind, string> = {
  ran: "Ran without a prompt",
  approved: "Approved at a prompt",
  failed: "Failed",
  approved_failed: "Approved at a prompt, then failed",
  denied_by_auto_mode: "Denied by auto mode",
  denied_by_user: "Denied at the prompt",
  unknown: "Unknown or still running",
};

// Everything here is captured from the agent, so it is only ever rendered as text: React escapes
// `{...}` values. Never use dangerouslySetInnerHTML in this file.
export function FlagItem({ flag }: { flag: Flag }) {
  return (
    <li className={`flag ${flag.severity}`}>
      <div className="what-ran">
        <span className="tool">{flag.tool}</span> <code>{flag.target}</code>
      </div>
      {flag.stated_reason && <p className="reason">{flag.stated_reason}</p>}
      <p className={`outcome ${flag.outcome.kind}`}>
        Outcome: <strong>{OUTCOMES[flag.outcome.kind]}</strong>
      </p>
      {flag.outcome.error && <pre className="outcome-error">{flag.outcome.error}</pre>}
      <div className="tags">
        <span className="rule">{flag.rule}</span>
        <span className="severity">{flag.severity}</span>
        {flag.label && <span className="label">{flag.label}</span>}
      </div>
      {flag.label === "partial" && (
        <p className="caveat">
          Shell network access is not fully visible: only http(s) URLs written in the command were
          checked, and a URL may only be text.
        </p>
      )}
      <ul className="evidence">
        {Object.entries(flag.evidence).map(([key, value]) => (
          <li key={key}>{`${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`}</li>
        ))}
      </ul>
      <div className="where">
        <time dateTime={flag.time}>{new Date(flag.time).toLocaleString()}</time>
        <span className="project">{flag.project}</span>
      </div>
    </li>
  );
}
