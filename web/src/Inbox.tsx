import { useEffect, useState } from "react";
import {
  acknowledgeFlag,
  type Digest,
  dismissFlag,
  fetchDigest,
  markAllReviewed,
  suppressFlag,
} from "./api";
import { FlagItem } from "./FlagItem";

export function Inbox() {
  const [digest, setDigest] = useState<Digest>();
  const [error, setError] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [busy, setBusy] = useState(false);

  // Loading only reads: the page changes review state only when a button is clicked.
  useEffect(() => {
    fetchDigest().then(setDigest, (e: Error) => setError(e.message));
  }, []);

  // Reload after every action, even a failed one: a 409 means the flag already changed. If the
  // reload fails, the last inbox stays on screen with the problem above it.
  // Returns the problem too, for a form to show next to itself (the top may be out of view).
  async function act(action: () => Promise<void>): Promise<string | undefined> {
    setBusy(true);
    setActionError(undefined);
    const problems = [];
    try {
      await action();
    } catch (e) {
      problems.push(`Couldn't save: ${(e as Error).message}`);
    }
    try {
      setDigest(await fetchDigest());
    } catch (e) {
      problems.push(`Couldn't reload: ${(e as Error).message}`);
    }
    const problem = problems.join(" · ") || undefined;
    setActionError(problem);
    setBusy(false);
    return problem;
  }

  if (error) return <p className="error">Couldn't load the digest: {error}</p>;
  if (!digest) return <p>Loading…</p>;
  const { actions, captured, need_review, suppressed, since } = digest.headline;
  return (
    <main>
      <p>
        <a href="#/suppressions">Suppressions →</a>
      </p>
      <h1>
        {`${actions} actions · ${captured} captured · ${need_review} need review`}
        {suppressed > 0 && (
          <>
            {" · "}
            <a href="#/suppressions">{`${suppressed} suppressed`}</a>
          </>
        )}
      </h1>
      {since && (
        <p className="since">
          Since your last review, <time dateTime={since}>{new Date(since).toLocaleString()}</time>
        </p>
      )}
      {actionError && <p className="error">{actionError}</p>}
      {need_review === 0 && <p>Nothing needs review.</p>}
      {/* Also with no flags left (each cleared by hand), so the window can still be reset. */}
      {(need_review > 0 || actions > 0) && (
        <button type="button" disabled={busy} onClick={() => act(() => markAllReviewed(digest))}>
          Mark all reviewed
        </button>
      )}
      {digest.projects.map(({ project, sessions }) => (
        <section key={project ?? ""}>
          <h2>{project ?? "No project"}</h2>
          {sessions.map(({ session_id, flags }) => (
            <section key={session_id ?? ""}>
              <h3>{`Session ${session_id?.slice(0, 8) ?? "unknown"}`}</h3>
              <ul className="flags">
                {flags.map((flag) => (
                  <FlagItem
                    key={flag.id}
                    flag={flag}
                    busy={busy}
                    onDismiss={() => act(() => dismissFlag(flag.id))}
                    onSuppress={(pattern, note) => act(() => suppressFlag(flag.id, pattern, note))}
                    onAcknowledge={() => act(() => acknowledgeFlag(flag.id))}
                  />
                ))}
              </ul>
            </section>
          ))}
        </section>
      ))}
    </main>
  );
}
