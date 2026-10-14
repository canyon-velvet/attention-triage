import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Digest } from "./api";
import { Inbox } from "./Inbox";
import { flag } from "./testing";

const AS_OF = "2026-10-12T09:00:00.000+00:00";

function digest(flags = [flag], since: string | null = null, actions = 214): Digest {
  return {
    as_of: AS_OF,
    capture_problems: [],
    headline: { actions, need_review: flags.length, suppressed: 0, since },
    projects: flags.length
      ? [{ project: "/p/main", sessions: [{ session_id: "73eb2355-abcd", flags }] }]
      : [],
  };
}

/** Each load gets the next digest (the last one repeats; null = HTTP 500). Returns every request. */
function serve(digests: (Digest | null)[], postStatus = 200, postDetail?: unknown) {
  const requests: { method: string; url: string; body?: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      requests.push({ method, url, body: init?.body && JSON.parse(init.body as string) });
      if (method === "POST") {
        return { ok: postStatus === 200, status: postStatus, json: async () => ({ detail: postDetail }) };
      }
      const next = digests.length > 1 ? digests.shift() : digests[0];
      return next ? { ok: true, json: async () => next } : { ok: false, status: 500 };
    }),
  );
  return requests;
}

test("the inbox shows the headline and flags grouped by project and session", async () => {
  serve([digest()]);
  render(<Inbox />);
  expect(await screen.findByText("214 actions · 1 need review")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull(); // capture looks healthy
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
  expect(requests[1].body).toEqual({ flag_ids: [], as_of: AS_OF, new_window: true });
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
    body: { flag_ids: [1, 2], as_of: AS_OF, new_window: true },
  });
});

test("a failed action says so, and the inbox reloads", async () => {
  const requests = serve([digest()], 409);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
  expect(await screen.findByText("Couldn't save: HTTP 409")).toBeInTheDocument();
  expect(requests.map((r) => r.method)).toEqual(["GET", "POST", "GET"]);
});

test("fine for future ones saves a suppression, then the inbox reloads", async () => {
  const requests = serve([digest(), digest([])]);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Fine — this and future ones" }));
  fireEvent.click(screen.getByRole("button", { name: "Don't flag again" }));
  await screen.findByText("Nothing needs review.");
  expect(requests[1]).toEqual({
    method: "POST",
    url: "/api/flags/1/dismiss",
    body: { scope: "future", pattern: null, note: null },
  });
});

test("a refused pattern shows the server's reason", async () => {
  serve([digest()], 422, "`rm` can only match exactly");
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Fine — this and future ones" }));
  fireEvent.click(screen.getByRole("button", { name: "Don't flag again" }));
  // above the list, and in the form, which may be far below it
  const reason = "Couldn't save: HTTP 422: `rm` can only match exactly";
  expect(await screen.findAllByText(reason)).toHaveLength(2);
});

test("the Suppressions page is always one click away, even with nothing hidden lately", async () => {
  serve([digest()]);
  render(<Inbox />);
  expect(await screen.findByRole("link", { name: "Suppressions →" })).toHaveAttribute("href", "#/suppressions");
});

test("the headline counts what was suppressed, linking to the Suppressions page", async () => {
  const hidden = digest();
  hidden.headline.suppressed = 3;
  serve([hidden]);
  render(<Inbox />);
  const headline = await screen.findByRole("heading", { level: 1 });
  expect(headline).toHaveTextContent("214 actions · 1 need review · 3 suppressed");
  expect(screen.getByRole("link", { name: "3 suppressed" })).toHaveAttribute("href", "#/suppressions");
});

test("if the reload after an action fails, the last inbox stays with the problem shown", async () => {
  serve([digest(), null]);
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
  expect(await screen.findByText("Couldn't reload: HTTP 500")).toBeInTheDocument();
  expect(screen.getByText("rm -rf build")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Acknowledge" })).toBeEnabled();
});

test("a capture problem is shown above the headline, with what to run", async () => {
  const broken = digest();
  broken.capture_problems = ["Hooks missing from /home/me/.claude/settings.json: PostToolUse"];
  serve([broken]);
  render(<Inbox />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Actions may be missing from this inbox. Run `triage doctor` for the fix.");
  expect(alert).toHaveTextContent("Hooks missing from /home/me/.claude/settings.json: PostToolUse");
});

function many(): Digest {
  const bypass = (id: number, target: string) => ({ ...flag, id, target });
  const domain = (id: number, target: string) => ({ ...flag, id, target, rule: "unlisted_domain" });
  return {
    ...digest(),
    headline: { actions: 50, need_review: 4, suppressed: 0, since: null },
    projects: [
      { project: "/p/main", sessions: [{ session_id: "s1", flags: [bypass(1, "Docker PS"), domain(2, "curl https://x.dev")] }] },
      { project: "/p/other", sessions: [{ session_id: "s2", flags: [bypass(3, "Docker run"), bypass(4, "make")] }] },
    ],
  };
}

const items = () => screen.getAllByRole("listitem").filter((li) => li.className.startsWith("flag"));

test("flags narrow by rule, project and what ran, and the headline says how many are shown", async () => {
  serve([many()]);
  render(<Inbox />);
  fireEvent.change(await screen.findByRole("combobox", { name: "Rule" }), { target: { value: "sandbox_bypass" } });
  expect(items()).toHaveLength(3);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("50 actions · 3 shown of 4 need review");
  fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "/p/other" } });
  expect(items()).toHaveLength(2);
  fireEvent.change(screen.getByRole("searchbox", { name: "What ran contains" }), { target: { value: "DOCKER" } });
  expect(items()).toHaveLength(1); // case-insensitive both ways: "Docker run" in /p/other
  expect(screen.queryByRole("heading", { name: "/p/main" })).toBeNull();
  fireEvent.change(screen.getByRole("searchbox", { name: "What ran contains" }), { target: { value: "<b>" } });
  expect(screen.getByText("No flags match these filters.")).toBeInTheDocument();
});

test("clearing the filters brings back the full inbox", async () => {
  serve([many()]);
  render(<Inbox />);
  fireEvent.change(await screen.findByRole("searchbox", { name: "What ran contains" }), { target: { value: "make" } });
  expect(items()).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  expect(items()).toHaveLength(4);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("50 actions · 4 need review");
  expect(screen.getByRole("button", { name: "Mark all reviewed" })).toBeInTheDocument();
});

test("with a filter on, marking reviewed sends only the shown flags and keeps the window", async () => {
  const requests = serve([many()]);
  render(<Inbox />);
  fireEvent.change(await screen.findByRole("combobox", { name: "Rule" }), { target: { value: "unlisted_domain" } });
  fireEvent.click(screen.getByRole("button", { name: "Mark 1 shown reviewed" }));
  await waitFor(() => expect(requests).toHaveLength(3)); // saved, then reloaded
  expect(requests[1].body).toEqual({ flag_ids: [2], as_of: AS_OF, new_window: false });
});

test("a filter that matched everything stays clearable after its flags are marked", async () => {
  const empty = { ...many(), headline: { ...many().headline, need_review: 0 }, projects: [] };
  const requests = serve([many(), empty]);
  render(<Inbox />);
  fireEvent.change(await screen.findByRole("searchbox", { name: "What ran contains" }), { target: { value: "a" } });
  fireEvent.click(screen.getByRole("button", { name: /^Mark \d+ shown reviewed$/ }));
  await waitFor(() => expect(requests).toHaveLength(3));
  fireEvent.click(await screen.findByRole("button", { name: "Clear filters" }));
  expect(screen.getByRole("button", { name: "Mark all reviewed" })).toBeInTheDocument(); // window resettable
});

test("a rule whose flags are all gone stops filtering", async () => {
  const left = many();
  left.projects = left.projects.map((p) => ({
    ...p,
    sessions: p.sessions.map((s) => ({ ...s, flags: s.flags.filter((f) => f.rule !== "unlisted_domain") })),
  }));
  serve([many(), left]);
  render(<Inbox />);
  fireEvent.change(await screen.findByRole("combobox", { name: "Rule" }), { target: { value: "unlisted_domain" } });
  fireEvent.click(screen.getByRole("button", { name: "Mark 1 shown reviewed" }));
  await waitFor(() => expect(items()).toHaveLength(3)); // all that's left, not "0 shown"
  expect(screen.getByRole("combobox", { name: "Rule" })).toHaveValue("");
});

test("flags without a project can be filtered to", async () => {
  const mixed = many();
  mixed.projects.push({ project: null, sessions: [{ session_id: "s3", flags: [{ ...flag, id: 9, target: "ls" }] }] });
  serve([mixed]);
  render(<Inbox />);
  const select = await screen.findByRole("combobox", { name: "Project" });
  const none = screen.getByRole("option", { name: "No project" }) as HTMLOptionElement;
  fireEvent.change(select, { target: { value: none.value } });
  expect(items()).toHaveLength(1);
  expect(screen.getByText("ls")).toBeInTheDocument();
});
