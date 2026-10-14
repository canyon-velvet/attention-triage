import type { Digest } from "./api";

export type Filter = { rule: string; project: string; text: string }; // "" = any
export const NO_FILTER: Filter = { rule: "", project: "", text: "" };
// The project value of flags with none: "" already means any project.
export const NO_PROJECT = "\u0000none";
export const projectValue = (project: string | null) => project ?? NO_PROJECT;

// The filter as this digest can apply it: a rule or project it no longer has (all its flags were
// just cleared) is dropped, so it can't keep hiding everything from a control that can't show it.
export function live(digest: Digest, filter: Filter): Filter {
  const flags = digest.projects.flatMap((p) => p.sessions.flatMap((s) => s.flags));
  return {
    ...filter,
    rule: flags.some((f) => f.rule === filter.rule) ? filter.rule : "",
    project: digest.projects.some((p) => projectValue(p.project) === filter.project)
      ? filter.project
      : "",
  };
}

// The digest narrowed to the flags that match: by rule, by project (the inbox's grouping), and by
// text in what ran, ignoring case. Empty sessions and projects are left out.
export function narrow(digest: Digest, { rule, project, text }: Filter): Digest {
  const needle = text.trim().toLowerCase();
  const projects = digest.projects
    .filter((p) => !project || projectValue(p.project) === project)
    .map((p) => ({
      ...p,
      sessions: p.sessions
        .map((s) => ({
          ...s,
          flags: s.flags.filter(
            (f) =>
              (!rule || f.rule === rule) &&
              (!needle || (f.target ?? "").toLowerCase().includes(needle)),
          ),
        }))
        .filter((s) => s.flags.length > 0),
    }))
    .filter((p) => p.sessions.length > 0);
  return { ...digest, projects };
}

export const count = (digest: Digest) =>
  digest.projects.reduce((n, p) => n + p.sessions.reduce((m, s) => m + s.flags.length, 0), 0);
