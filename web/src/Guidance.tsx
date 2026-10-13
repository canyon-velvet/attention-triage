import { useState } from "react";
import type { Guidance as GuidanceData } from "./api";

// "Fix it" for one inbox item: what to look at and copy-ready commands. Triage runs none of them
// (ADR-0002); the server built and quoted them. Captured text renders as text only.
export function Guidance({ guidance }: { guidance: GuidanceData }) {
  const { files, commits, policy } = guidance;
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
      <h4>Policy</h4>
      <p>
        {policy.hint} <span className="caveat">Setting <code>{policy.key}</code> in {policy.file}</span>
      </p>
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
