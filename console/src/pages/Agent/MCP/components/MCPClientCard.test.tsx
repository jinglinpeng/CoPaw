import type { ReactNode, MouseEventHandler } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MCPClientInfo } from "../../../../api/types";
import { MCPClientCard } from "./MCPClientCard";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../../../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));
vi.mock("./MCPAccessModal", () => ({ MCPAccessModal: () => null }));
vi.mock("./MCPOAuthSection", () => ({ MCPOAuthSection: () => null }));
vi.mock("@agentscope-ai/design", () => ({
  Card: ({
    children,
    onClick,
  }: {
    children: ReactNode;
    onClick: MouseEventHandler;
  }) => <div onClick={onClick}>{children}</div>,
  Button: ({
    children,
    disabled,
    onClick,
  }: {
    children: ReactNode;
    disabled?: boolean;
    onClick: MouseEventHandler<HTMLButtonElement>;
  }) => (
    <button disabled={disabled} onClick={onClick}>
      {children}
    </button>
  ),
  Tooltip: ({ children }: { children: ReactNode }) => children,
  Modal: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div role="dialog">{children}</div> : null,
  Input: { TextArea: () => null },
}));

const client = {
  key: "echo",
  name: "Echo",
  enabled: true,
  transport: "stdio",
  command: "echo",
} as MCPClientInfo;

describe("MCPClientCard runtime status", () => {
  it.each(["connecting", "active", "inactive", "error"] as const)(
    "renders %s and keeps it out of editable configuration",
    (status) => {
      render(
        <MCPClientCard
          client={{ ...client, runtime_status: status }}
          onToggle={vi.fn()}
          onDelete={vi.fn()}
          onUpdate={vi.fn()}
          onUpdatePolicy={vi.fn()}
        />,
      );
      expect(screen.getByText(`mcp.runtime.${status}`)).toBeInTheDocument();
      const tools = screen.getByRole("button", { name: "mcp.tools" });
      if (status === "connecting") expect(tools).toBeDisabled();
      else expect(tools).not.toBeDisabled();
      fireEvent.click(screen.getByText("Echo"));
      expect(screen.getByRole("dialog")).not.toHaveTextContent(
        "runtime_status",
      );
      expect(screen.getByRole("dialog")).toHaveTextContent('"command": "echo"');
    },
  );
});
