import { useState } from "react";
import type { Flag, Pattern } from "./api";

type Choice = { kind: Pattern["kind"]; label: string; value: string };

// What "edit pattern" offers (SPEC §6). Each widened choice starts at the narrowest value: the
// whole command, or the file's own folder. The server checks every pattern (suppress.py).
function choices(flag: Flag): Choice[] {
  const list: Choice[] = [{ kind: "exact", label: "Exactly this", value: "" }];
  // An unlisted_domain flag reviews hosts: a prefix would also cover hosts added later.
  if (flag.target_kind === "command" && flag.rule !== "unlisted_domain" && flag.target) {
    list.push({ kind: "prefix", label: "Commands starting with", value: flag.target });
  }
  const path = flag.evidence.path; // the path as the rule resolved it
  if (flag.target_kind === "path" && typeof path === "string") {
    const folder = path.slice(0, path.lastIndexOf("/"));
    list.push({ kind: "directory", label: "Anything in the folder", value: folder });
  }
  return list;
}

export function SuppressForm({
  flag,
  busy,
  onConfirm,
  onCancel,
}: {
  flag: Flag;
  busy: boolean;
  onConfirm: (pattern: Pattern | null, note: string) => Promise<string | undefined>; // null: exact
  onCancel: () => void;
}) {
  const options = choices(flag);
  const [kind, setKind] = useState<Pattern["kind"]>("exact");
  const [values, setValues] = useState(() => Object.fromEntries(options.map((o) => [o.kind, o.value])));
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string>();

  return (
    <form
      className="suppress"
      onSubmit={async (e) => {
        e.preventDefault();
        setProblem(await onConfirm(kind === "exact" ? null : { kind, value: values[kind] }, note));
      }}
    >
      <fieldset>
        <legend>Don't flag again in this project:</legend>
        {options.map((o) => (
          <div key={o.kind} className="choice">
            <label>
              <input
                type="radio"
                name={`suppress-${flag.id}`}
                checked={kind === o.kind}
                onChange={() => setKind(o.kind)}
              />{" "}
              {o.label}
            </label>
            {o.kind !== "exact" && (
              <input
                type="text"
                aria-label={o.label}
                value={values[o.kind]}
                disabled={kind !== o.kind}
                onChange={(e) => setValues({ ...values, [o.kind]: e.target.value })}
              />
            )}
          </div>
        ))}
      </fieldset>
      {kind === "prefix" && (
        <p className="caveat">
          A prefix trusts that program: whatever it runs later, with any arguments, won't be
          flagged here.
        </p>
      )}
      <label className="note">
        Note <input type="text" value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <div className="actions">
        <button type="submit" disabled={busy}>
          Don't flag again
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {problem && <p className="error">{problem}</p>}
    </form>
  );
}
