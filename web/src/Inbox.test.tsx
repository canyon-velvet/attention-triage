import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Digest } from "./api";
import { Inbox } from "./Inbox";
import { flag } from "./testing";

const AS_OF = "2026-10-12T09:00:00.000+00:00";

function digest(flags = [flag], since: string | null = null, actions = 214): Digest {
  return {
    as_of: AS_OF,
    headline: { actions, captured: actions, need_review: flags.length, since },
    projects: flags.length
      ? [{ project: "/p/main", sessions: [{ session_id: "73eb2355-abcd", flags }] }]
      : [],
  };
}

/** Each load gets the next digest (the last one repeats; null = HTTP 500). Returns every request. */
function serve(digests: (Digest | null)[], postStatus = 200) {
  const requests: { method: string; url: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      requests.push({ method, url, body: init?.body && JSON.parse(init.body as string) });
      if (method === "POST") return { ok: postStatus === 200, status: postStatus };
      const next = digests.length > 1 ? digests.shift() : digests[0];
      return next ? { ok: true, json: async () => next } : { ok: false, status: 500 };
    }),
  );
  return requests;
}

test("the inbox shows the headline and flags grouped by project and session", async () => {
  serve([digest()]);
  render(<Inbox />);
  expect(await screen.findByText("214 actions · 214 captured · 1 need review")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "/p/main" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Session 73eb2355" })).toBeInTheDocument();
  expect(screen.getByText("rm -rf build")).toBeInTheDocument();
  expect(screen.queryByText(/since your last review/i)).toBeNull();
});

test("the inbox says when nothing needs review, and offers no review when nothing happened", async () => {
  serve([digest([], null, 0)]);
  render(<Inbox />);
  expect(await screen.findByText("Nothing needs review.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Mark all reviewed" })).toBeNull();
});

test("with every flag cleared by hand, mark all reviewed still resets the window", async () => {
  const requests = serve([digest([])]);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Mark all reviewed" }));
  await waitFor(() => expect(requests).toHaveLength(3)); // saved, then reloaded
  expect(requests[1].body).toEqual({ flag_ids: [], as_of: AS_OF });
});

test("the inbox says when the digest can't be loaded", async () => {
  serve([null]);
  render(<Inbox />);
  expect(await screen.findByText("Couldn't load the digest: HTTP 500")).toBeInTheDocument();
});

test("the headline says when the last review was", async () => {
  serve([digest([flag], "2026-10-11T18:30:00.000+00:00")]);
  render(<Inbox />);
  const line = await screen.findByText(/since your last review/i);
  expect(line.querySelector("time")).toHaveAttribute("datetime", "2026-10-11T18:30:00.000+00:00");
});

test("opening the inbox only reads", async () => {
  const requests = serve([digest()]);
  render(<Inbox />);
  await screen.findByText("rm -rf build");
  expect(requests.map((r) => r.method)).toEqual(["GET"]);
});

test.each([
  ["Fine — this one only", "/api/flags/1/dismiss", { scope: "once" }],
  ["Acknowledge", "/api/flags/1/ack", {}],
])("%s saves it, then the inbox reloads without the flag", async (button, url, body) => {
  const requests = serve([digest(), digest([])]);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: button }));
  expect(await screen.findByText("Nothing needs review.")).toBeInTheDocument();
  expect(requests).toEqual([
    { method: "GET", url: "/api/digest", body: undefined },
    { method: "POST", url, body },
    { method: "GET", url: "/api/digest", body: undefined },
  ]);
});

test("mark all reviewed sends the flags shown and when the page loaded", async () => {
  const requests = serve([digest([flag, { ...flag, id: 2, target: "ls" }]), digest([])]);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Mark all reviewed" }));
  await screen.findByText("Nothing needs review.");
  expect(requests[1]).toEqual({
    method: "POST",
    url: "/api/review/mark-all",
    body: { flag_ids: [1, 2], as_of: AS_OF },
  });
});

test("a failed action says so, and the inbox reloads", async () => {
  const requests = serve([digest()], 409);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
  expect(await screen.findByText("Couldn't save: HTTP 409")).toBeInTheDocument();
  expect(requests.map((r) => r.method)).toEqual(["GET", "POST", "GET"]);
});

test("if the reload after an action fails, the last inbox stays with the problem shown", async () => {
  serve([digest(), null]);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
  expect(await screen.findByText("Couldn't reload: HTTP 500")).toBeInTheDocument();
  expect(screen.getByText("rm -rf build")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Acknowledge" })).toBeEnabled();
});
