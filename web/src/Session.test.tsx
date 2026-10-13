import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { SessionDetail, SessionEvent } from "./api";
import { Session, sessionId } from "./Session";
import "./testing"; // unmounts each render after its test

const row = (event: Partial<SessionEvent>): SessionEvent => ({
  event_type: "tool_call",
  tool: null,
  target_kind: null,
  target: null,
  stated_reason: null,
  project: "/p/main",
  time: "2026-10-09T15:13:12.434+00:00",
  outcome: null,
  flags: [],
  ...event,
});

const session: SessionDetail = {
  session_id: "73eb2355-abcd",
  events: [
    row({ event_type: "session_start" }),
    row({
      tool: "Bash",
      target: "rm -rf build",
      stated_reason: "Clean the build folder",
      outcome: { kind: "failed", error: "Operation not permitted" },
      flags: [
        { id: 4, rule: "sandbox_bypass", severity: "high", label: "", reason: "…", status: "suppressed" },
      ],
    }),
    row({ tool: "Read", target: "/p/main/README.md", outcome: { kind: "ran", error: null } }),
    row({ event_type: "tool_result", tool: "Bash", target: "ls" }),
  ],
  subagents: [
    {
      agent_id: "aaa1ae0a35159e3f1",
      agent_type: "Explore",
      events: [row({ event_type: "subagent_start" }), row({ tool: "Bash", target: "ls -la" })],
    },
  ],
};

function serve(body: SessionDetail | null) {
  const fetch = vi.fn(async () => (body ? { ok: true, json: async () => body } : { ok: false, status: 404 }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

test("every event is listed in order, flagged ones highlighted with their flags' status", async () => {
  const fetch = serve(session);
  render(<Session id="73eb2355-abcd" />);
  const [main] = await screen.findAllByRole("list");
  const rows = main.querySelectorAll(":scope > li");
  expect([...rows].map((r) => r.className)).toEqual(["event", "event flagged high", "event", "event"]);
  expect(rows[0]).toHaveTextContent("Session started");
  expect(rows[1]).toHaveTextContent("Bash rm -rf build · Failed");
  expect(rows[1]).toHaveTextContent("Operation not permitted");
  expect(rows[1]).toHaveTextContent("sandbox_bypass · high · suppressed");
  expect(rows[3]).toHaveTextContent("Result of a call that wasn't captured Bash ls");
  expect(fetch).toHaveBeenCalledWith("/api/sessions/73eb2355-abcd");
});

test("each subagent's events get a section of their own", async () => {
  serve(session);
  render(<Session id="73eb2355-abcd" />);
  const heading = await screen.findByRole("heading", { name: "Subagent Explore (aaa1ae0a)" });
  const section = heading.closest("section")!;
  expect(section).toHaveTextContent("Subagent started");
  expect(section).toHaveTextContent("Bash ls -la");
  expect(section).not.toHaveTextContent("rm -rf build");
});

test("a session that can't be loaded says so", async () => {
  serve(null);
  render(<Session id="gone" />);
  expect(await screen.findByText("Couldn't load the session: HTTP 404")).toBeInTheDocument();
});

test("a mangled id in the link is kept as typed instead of crashing the page", () => {
  expect(sessionId("73eb2355-abcd")).toBe("73eb2355-abcd");
  expect(sessionId("a%2Fb")).toBe("a/b");
  expect(sessionId("%E0%A4")).toBe("%E0%A4");
});

test("moving to another session drops the first one's late answer", async () => {
  let answerFirst: (body: unknown) => void = () => {};
  const other = { ...session, session_id: "b", events: [row({ event_type: "session_end" })], subagents: [] };
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      url.endsWith("/a")
        ? new Promise((resolve) => {
            answerFirst = (body) => resolve({ ok: true, json: async () => body });
          })
        : Promise.resolve({ ok: true, json: async () => other }),
    ),
  );
  const { rerender } = render(<Session id="a" />);
  rerender(<Session id="b" />);
  expect(await screen.findByText("Session ended")).toBeInTheDocument();
  answerFirst(session);
  await new Promise((resolve) => setTimeout(resolve, 20)); // let the late answer land
  expect(screen.queryByText("Session started")).toBeNull();
  expect(screen.getByText("Session ended")).toBeInTheDocument();
});
