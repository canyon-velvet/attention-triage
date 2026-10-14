import { useState } from "react";
import type { Guidance as GuidanceData } from "./api";

// "Fix it" for one inbox item: what its call changed, with copy-ready commands. Triage runs none of
// them (ADR-0002); the server built and quoted them. Captured text renders as text only. An item
// that changed no files and made no commits has nothing to fix here, so it shows nothing.
export function Guidance({ guidance }: { guidance: GuidanceData }) {
  const { files, commits } = guidance;
  if (!files.length && !commits.length) return null;
  return (
    <details className="guidance">
      <summary>Fix it</summary>
      {files.length > 0 && (
        <>
          <h4>Files it changed</h4>
          <ul>
            {files.map((f) => (
              <li key={f.path}>
                <code>{f.path}</code>
                {f.commands.length ? (
                  f.commands.map((c) => <Command key={c} text={c} />)
                ) : (
                  <p className="caveat">Not in this project's git checkout: check it by hand.</p>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {commits.length > 0 && (
        <>
          <h4>Commits since</h4>
          <ul>
            {commits.map((c) => (
              <li key={c.command}>
                <code>{c.sha}</code> {c.subject}
                <Command text={c.command} />
              </li>
            ))}
          </ul>
        </>
      )}
    </details>
  );
}

function Command({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="command">
      <code>{text}</code>
      <button
        type="button"
        onClick={() =>
          // Unavailable without a secure context; the command can still be selected by hand.
          navigator.clipboard?.writeText(text).then(
            () => setCopied(true),
            () => {},
          )
        }
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
