import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import Sender from "@agentscope-ai/chat/lib/Sender";
import { useState } from "react";
import RichFileReferenceInput, {
  RichFileReferenceInputProvider,
} from "./RichFileReferenceInput";
import { buildMcpSlashSuggestions } from "./mcpSlash";
import "../../i18n";

// Use the real Sender and Suggestion, with only the unrelated package barrel
// stubbed to avoid importing every diagram and media renderer in this test.
vi.mock("@agentscope-ai/chat/lib", () => ({
  useProviderContext: () => ({
    direction: "ltr",
    getPrefixCls: (name: string) => `test-${name}`,
  }),
}));

beforeAll(() => {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
});

function Composer({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <RichFileReferenceInputProvider
      onOpenReference={vi.fn()}
      mcpNames={{ echo: "Echo Server" }}
    >
      <Sender
        value={value}
        onChange={setValue}
        suggestions={buildMcpSlashSuggestions([
          { key: "echo", name: "Echo Server", description: "", enabled: true },
        ])}
        components={{ input: RichFileReferenceInput }}
      />
    </RichFileReferenceInputProvider>
  );
}

describe("MCP selection with the installed Sender", () => {
  it.each(["mouse", "keyboard"])(
    "selects a concrete MCP by %s and renders an atomic chip",
    async (method) => {
      const { container } = render(<Composer />);
      const editor = screen.getByRole("textbox");
      editor.focus();
      fireEvent.change(container.querySelector("textarea")!, {
        target: { value: "/" },
      });
      const option = await screen.findByText("/Echo Server · MCP");
      if (method === "mouse") fireEvent.click(option);
      else {
        fireEvent.keyDown(editor, { key: "ArrowDown", code: "ArrowDown" });
        fireEvent.keyDown(editor, { key: "Enter", code: "Enter" });
      }
      expect(
        await screen.findByRole("button", { name: "Echo Server · MCP" }),
      ).toBeInTheDocument();
      await waitFor(() =>
        expect(container.querySelector("textarea")).toHaveValue("/mcp:echo "),
      );
      expect(editor).not.toHaveTextContent("/mcp:echo");
    },
  );

  it("searches the display name even when it differs from the stable key", async () => {
    const { container } = render(<Composer />);
    const editor = screen.getByRole("textbox");
    editor.focus();
    fireEvent.change(container.querySelector("textarea")!, {
      target: { value: "/Echo" },
    });
    expect(await screen.findByText("/Echo Server · MCP")).toBeInTheDocument();
  });

  it("restores a draft chip and removes its selection atomically", async () => {
    const { container } = render(<Composer initial="/mcp:echo task" />);
    await screen.findByRole("button", { name: "Echo Server · MCP" });
    const textarea = container.querySelector("textarea")!;
    await act(async () => {
      textarea.setSelectionRange(9, 9);
      fireEvent.focus(textarea);
    });
    fireEvent.keyDown(screen.getByRole("textbox"), {
      key: "Backspace",
      code: "Backspace",
    });
    await waitFor(() =>
      expect(container.querySelector("textarea")).toHaveValue(" task"),
    );
    expect(
      screen.queryByRole("button", { name: "Echo Server · MCP" }),
    ).not.toBeInTheDocument();
  });
});
