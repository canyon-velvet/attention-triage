import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import type { Flag } from "./api";

export const flag: Flag = {
  id: 1,
  tool: "Bash",
  target_kind: "command",
  target: "rm -rf build",
  stated_reason: "Clean the build folder",
  rule: "sandbox_bypass",
  severity: "high",
  label: "preemptive",
  evidence: { command: "rm -rf build" },
  time: "2026-10-09T15:13:12.434+00:00",
  project: "/p/main/.claude/worktrees/wt",
  outcome: { kind: "ran", error: null },
};

afterEach(() => {
  cleanup(); // unmount the last render (automatic only with vitest globals)
  vi.unstubAllGlobals();
});
