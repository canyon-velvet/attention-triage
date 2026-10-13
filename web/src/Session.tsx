import { useEffect, useState } from "react";
import { fetchSession, type SessionDetail, type SessionEvent } from "./api";
import { OUTCOMES } from "./FlagItem";

// Rows that aren't a tool call. The last three are results whose call was never stored.
const EVENTS: Record<string, string> = {
  session_start: "Session started",
  turn_end: "Turn ended",
  session_end: "Session ended",
  subagent_start: "Subagent started",
  subagent_stop: "Subagent finished",
  tool_result: "Result of a call that wasn't captured",
  tool_failure: "Failure of a call that wasn't captured",
  permission_denied: "Denial of a call that wasn't captured",
};

export const sessionLink = (id: string) => `#/sessions/${encodeURIComponent(id)}`;

// The id in a #/sessions/ link; a mangled one (`%` alone) stays as typed and loads as a 404.
export function sessionId(encoded: string): string {
  try {
    return decodeURIComponent(encoded);
  } catch {
    return encoded;
  }
}

// Every captured event of one session, flagged ones highlighted. Captured text renders as text only.
export function Session({ id }: { id: string }) {
  const [session, setSession] = useState<SessionDetail>();
  const [error, setError] = useState<string>();

  // A new id (the # edited by hand) starts afresh, and the old id's late answer is dropped.
  useEffect(() => {
    let current = true;
    setSession(undefined);
    setError(undefined);
    fetchSession(id).then(
      (found) => current && setSession(found),
      (e: Error) => current && setError(e.message),
    );
    return () => {
      current = false;
    };
  }, [id]);

  return (
    <main>
      <p>
        <a href="#/">← Inbox</a>
      </p>
      <h1>{`Session ${id.slice(0, 8)}`}</h1>
      {error && <p className="error">Couldn't load the session: {error}</p>}
      {session && (
        <>
          <Events events={session.events} />
          {session.subagents.map((s) => (
            <section key={s.agent_id}>
              <h2>{`Subagent${s.agent_type ? ` ${s.agent_type}` : ""} (${s.agent_id.slice(0, 8)})`}</h2>
              <Events events={s.events} />
            </section>
          ))}
        </>
      )}
    </main>
  );
}

function Events({ events }: { events: SessionEvent[] }) {
  return (
    <ol className="events">
      {events.map((e, i) => {
        const severity = e.flags.some((f) => f.severity === "high") ? "high" : "review";
        return (
          // rows have no id of their own, and the list never reorders
          <li key={i} className={e.flags.length ? `event flagged ${severity}` : "event"}>
            <time dateTime={e.time}>{new Date(e.time).toLocaleTimeString()}</time>{" "}
            {e.event_type === "tool_call" ? (
              <>
                <span className="tool">{e.tool}</span> <code>{e.target}</code>
                {e.outcome && <span className="outcome"> · {OUTCOMES[e.outcome.kind]}</span>}
              </>
            ) : (
              <>
                {EVENTS[e.event_type] ?? e.event_type} {e.tool && <span className="tool">{e.tool}</span>}{" "}
                {e.target && <code>{e.target}</code>}
              </>
            )}
            {e.outcome?.error && <pre className="outcome-error">{e.outcome.error}</pre>}
            {e.stated_reason && <p className="reason">{e.stated_reason}</p>}
            {e.flags.length > 0 && (
              <div className="tags">
                {e.flags.map((f) => (
                  <span key={f.id}>{`${f.rule} · ${f.severity} · ${f.status}`}</span>
                ))}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
