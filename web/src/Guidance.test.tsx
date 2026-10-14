import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Guidance as GuidanceData } from "./api";
import { Guidance } from "./Guidance";
import { flag } from "./testing";

const full: GuidanceData = {
  files: [
    {
      path: "/p/main/src/a b.py",
      commands: ["git -C /p/main diff -- 'src/a b.py'", "git -C /p/main restore -- 'src/a b.py'"],
    },
    { path: "/srv/notes.txt", commands: [] },
  ],
  commits: [{ sha: "1a2b3c4", subject: "Fix it", command: "git -C /p/main revert 1a2b3c4" }],
};

test("each changed file in the checkout gets its commands, others say to check by hand", () => {
  render(<Guidance guidance={full} />);
  expect(screen.getByText("git -C /p/main diff -- 'src/a b.py'")).toBeInTheDocument();
  expect(screen.getByText("git -C /p/main restore -- 'src/a b.py'")).toBeInTheDocument();
  const outside = screen.getByText("/srv/notes.txt").closest("li")!;
  expect(outside).toHaveTextContent("Not in this project's git checkout: check it by hand.");
  expect(outside.querySelector("button")).toBeNull();
});

test("later commits get a revert command", () => {
  render(<Guidance guidance={full} />);
  const commit = screen.getByText("1a2b3c4").closest("li")!;
  expect(commit).toHaveTextContent("1a2b3c4 Fix it");
  expect(commit).toHaveTextContent("git -C /p/main revert 1a2b3c4");
});

test("copy puts the command on the clipboard", async () => {
  const writeText = vi.fn(async () => {});
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<Guidance guidance={full} />);
  const [first] = screen.getAllByRole("button", { name: "Copy" });
  fireEvent.click(first);
  expect(writeText).toHaveBeenCalledWith("git -C /p/main diff -- 'src/a b.py'");
  expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
});

test("an item that changed nothing has no Fix it section", () => {
  const { container } = render(<Guidance guidance={flag.guidance} />);
  expect(container).toBeEmptyDOMElement();
});
