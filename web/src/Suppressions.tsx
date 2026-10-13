import { useEffect, useState } from "react";
import { fetchSuppressions, type Suppression, undoSuppression } from "./api";

const when = (ts: string) => <time dateTime={ts}>{new Date(ts).toLocaleString()}</time>;

// Every "Fine → this and future ones", with how often it hid a flag, and undo.
export function Suppressions() {
  const [suppressions, setSuppressions] = useState<Suppression[]>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetchSuppressions().then(setSuppressions, (e: Error) => setError(`Couldn't load: ${e.message}`));
  }, []);

  // Reload even after a failed undo: a 404 means it was already undone.
  async function undo(id: number) {
    setBusy(true);
    setError(undefined);
    const problems = [];
    try {
      await undoSuppression(id);
    } catch (e) {
      problems.push(`Couldn't undo: ${(e as Error).message}`);
    }
    try {
      setSuppressions(await fetchSuppressions());
    } catch (e) {
      problems.push(`Couldn't reload: ${(e as Error).message}`);
    }
    setError(problems.join(" · ") || undefined);
    setBusy(false);
  }

  return (
    <main>
      <p>
        <a href="#/">← Inbox</a>
      </p>
      <h1>Suppressions</h1>
      {error && <p className="error">{error}</p>}
      {suppressions?.length === 0 && <p>Nothing is suppressed.</p>}
      <ul className="flags">
        {suppressions?.map((s) => (
          <li key={s.id} className="flag">
            <p>
              <code>{s.pattern}</code>
              {s.kind === "exact" ? "" : ` (${s.kind})`} · {s.rule} · {s.project ?? "No project"}
            </p>
            {s.note && <p>{s.note}</p>}
            <p>
              Created {when(s.created_at)} · {s.hits} hits
              {s.last_hit && <> · last {when(s.last_hit)}</>}
            </p>
            {s.suggestion && <p className="suggestion">{s.suggestion}</p>}
            <button type="button" disabled={busy} onClick={() => undo(s.id)}>
              Undo
            </button>
          </li>
        ))}
      </ul>
    </main>
  );
}
