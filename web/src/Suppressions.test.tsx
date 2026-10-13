import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Suppression } from "./api";
import { Suppressions } from "./Suppressions";
import "./testing"; // unmounts each render after its test

const docker: Suppression = {
  id: 7,
  rule: "sandbox_bypass",
  kind: "prefix",
  pattern: "docker",
  project: "/p/main",
  note: "local containers",
  created_at: "2026-10-09T15:13:12.434+00:00",
  hits: 14,
  last_hit: "2026-10-12T08:00:00.000+00:00",
  suggestion: "matched 14× this week — consider adding `docker *` to sandbox `excludedCommands`.",
};

/** Each load gets the next list (the last one repeats). Returns every request. */
function serve(lists: Suppression[][], deleteStatus = 200) {
  const requests: { method: string; url: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      requests.push({ method, url });
      if (method === "DELETE") return { ok: deleteStatus === 200, status: deleteStatus, json: async () => ({}) };
      const next = lists.length > 1 ? lists.shift() : lists[0];
      return { ok: true, json: async () => next };
    }),
  );
  return requests;
}

test("each suppression shows its pattern, hits and config suggestion", async () => {
  serve([[docker]]);
  render(<Suppressions />);
  expect(await screen.findByText("docker")).toBeInTheDocument();
  expect(screen.getByText(/^Commands starting with:/)).toBeInTheDocument();
  expect(screen.getByText("Rule: sandbox_bypass · Project: /p/main")).toBeInTheDocument();
  expect(screen.getByText("Note: local containers")).toBeInTheDocument();
  expect(screen.getByText(/14 hits/)).toBeInTheDocument();
  expect(screen.getByText(docker.suggestion!)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "← Inbox" })).toHaveAttribute("href", "#/");
});

test("undo deletes the suppression, then the list reloads", async () => {
  const requests = serve([[docker], []]);
  render(<Suppressions />);
  fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
  expect(await screen.findByText("Nothing is suppressed.")).toBeInTheDocument();
  expect(requests[1]).toEqual({ method: "DELETE", url: "/api/suppressions/7" });
});

test("a failed undo says so and still reloads", async () => {
  const requests = serve([[docker]], 500);
  render(<Suppressions />);
  fireEvent.click(await screen.findByRole("button", { name: "Undo" }));
  expect(await screen.findByText("Couldn't undo: HTTP 500")).toBeInTheDocument();
  await waitFor(() => expect(requests).toHaveLength(3));
});

test("an exact suppression says so, and one hit is singular", async () => {
  serve([[{ ...docker, kind: "exact", pattern: "docker ps", hits: 1, note: null, suggestion: null }]]);
  render(<Suppressions />);
  expect(await screen.findByText(/^Exactly this:/)).toBeInTheDocument();
  expect(screen.getByText(/· 1 hit ·/)).toBeInTheDocument();
  expect(screen.queryByText(/^Note:/)).toBeNull();
});
