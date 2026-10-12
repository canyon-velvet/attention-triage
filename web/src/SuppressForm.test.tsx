import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { Flag } from "./api";
import { FlagItem } from "./FlagItem";
import { flag } from "./testing";

function open(item: Flag) {
  const onSuppress = vi.fn();
  render(
    <FlagItem flag={item} busy={false} onDismiss={() => {}} onAcknowledge={() => {}} onSuppress={onSuppress} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Fine — this and future ones" }));
  return onSuppress;
}

const choices = () => screen.getAllByRole("radio").map((r) => r.closest("label")?.textContent?.trim());

test("exactly this is the default", () => {
  const onSuppress = open(flag);
  expect(choices()).toEqual(["Exactly this", "Commands starting with"]);
  fireEvent.click(screen.getByRole("button", { name: "Don't flag again" }));
  expect(onSuppress).toHaveBeenCalledWith(null, "");
});

test("a command can be widened to a prefix, which says it trusts the program", () => {
  const onSuppress = open(flag);
  const prefix = screen.getByRole("textbox", { name: "Commands starting with" });
  expect(prefix).toHaveValue("rm -rf build"); // starts at the narrowest: the whole command
  expect(prefix).toBeDisabled();
  expect(screen.queryByText(/trusts that program/)).toBeNull();

  fireEvent.click(screen.getAllByRole("radio")[1]);
  expect(screen.getByText(/trusts that program/)).toBeInTheDocument();
  fireEvent.change(prefix, { target: { value: "rm -rf" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Note" }), { target: { value: "cleanup" } });
  fireEvent.click(screen.getByRole("button", { name: "Don't flag again" }));
  expect(onSuppress).toHaveBeenCalledWith({ kind: "prefix", value: "rm -rf" }, "cleanup");
});

test("a path can be widened to the folder its rule resolved", () => {
  const onSuppress = open({
    ...flag,
    tool: "Write",
    rule: "outside_project_write",
    target_kind: "path",
    target: "/srv/link/a.txt",
    evidence: { path: "/srv/data/a.txt" },
  });
  expect(choices()).toEqual(["Exactly this", "Anything in the folder"]);
  fireEvent.click(screen.getAllByRole("radio")[1]);
  expect(screen.getByRole("textbox", { name: "Anything in the folder" })).toHaveValue("/srv/data");
  fireEvent.click(screen.getByRole("button", { name: "Don't flag again" }));
  expect(onSuppress).toHaveBeenCalledWith({ kind: "directory", value: "/srv/data" }, "");
});

test("an unlisted domain flag offers only an exact match", () => {
  open({ ...flag, rule: "unlisted_domain", label: "partial" });
  expect(choices()).toEqual(["Exactly this"]);
});

test("a refused pattern's reason is shown in the form, which stays open", async () => {
  const onSuppress = open(flag);
  onSuppress.mockResolvedValue("Couldn't save: HTTP 422: `rm` can only match exactly");
  fireEvent.click(screen.getByRole("button", { name: "Don't flag again" }));
  const reason = await screen.findByText(/can only match exactly/);
  expect(reason.closest("form")).not.toBeNull();
  expect(screen.getAllByRole("radio")).toHaveLength(2);
});

test("cancel closes the form without saving", () => {
  const onSuppress = open(flag);
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("radio")).toBeNull();
  expect(onSuppress).not.toHaveBeenCalled();
});
