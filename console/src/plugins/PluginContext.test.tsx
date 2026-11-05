// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { loadAll } = vi.hoisted(() => ({ loadAll: vi.fn() }));
vi.mock("./hostExternals", () => ({
  pluginSystem: { getToolRenderConfig: () => ({}), subscribe: () => () => {} },
}));
vi.mock("./hostSdk/install", () => ({ installHostSdk: vi.fn() }));
vi.mock("../components/Chat/ToolCards/registerBuiltinCards", () => ({
  registerBuiltinCards: vi.fn(),
}));
vi.mock("react-i18next", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-i18next")>()),
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("./usePluginLoader", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./usePluginLoader")>()),
  loadAllPlugins: loadAll,
}));
import { PluginProvider, usePlugins } from "./PluginContext";
import { PluginInitializationError } from "./usePluginLoader";
function Consumer() {
  const state = usePlugins();
  return (
    <output data-testid="state">
      {JSON.stringify({ loading: state.loading, error: state.error })}
    </output>
  );
}
const state = () => JSON.parse(screen.getByTestId("state").textContent!);
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    await vi.dynamicImportSettled();
  });
}
function mount() {
  return render(
    <PluginProvider>
      <Consumer />
    </PluginProvider>,
  );
}
beforeEach(() => {
  vi.useFakeTimers();
  loadAll.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
describe("plugin initialization lifecycle", () => {
  it("waits beyond the former 15-second budget and recovers without refresh", async () => {
    let ready = false;
    loadAll.mockImplementation(async () => {
      if (!ready) throw new PluginInitializationError("starting", true, true);
      return { loaded: 1, failed: [] };
    });
    mount();
    await advance(1000);
    await advance(120000);
    expect(loadAll.mock.calls.length).toBeGreaterThan(5);
    expect(state()).toEqual({ loading: true, error: null });
    ready = true;
    await advance(5000);
    expect(state()).toEqual({ loading: false, error: null });
  });
  it("shows transport failure and resumes automatically after reconnection", async () => {
    loadAll
      .mockRejectedValueOnce(
        new PluginInitializationError("Connection refused", true),
      )
      .mockResolvedValue({ loaded: 1, failed: [] });
    mount();
    await advance(1000);
    expect(screen.getByRole("alert").textContent).toContain(
      "Connection refused",
    );
    await advance(1000);
    expect(state()).toEqual({ loading: false, error: null });
  });
  it.each(["401 denied", "permanent 500", "startup failed"])(
    "stops on %s and provides an actual retry button",
    async (message) => {
      loadAll.mockRejectedValue(new PluginInitializationError(message));
      mount();
      await advance(1000);
      await advance(120000);
      expect(loadAll).toHaveBeenCalledTimes(1);
      expect(state()).toEqual({ loading: false, error: message });
      loadAll.mockResolvedValue({ loaded: 1, failed: [] });
      fireEvent.click(screen.getByRole("button", { name: "common.retry" }));
      await advance(1000);
      expect(loadAll).toHaveBeenCalledTimes(2);
      expect(state().error).toBeNull();
    },
  );
  it("aborts the real operation signal and clears retries on unmount", async () => {
    let signal: AbortSignal | undefined;
    loadAll.mockImplementation((s: AbortSignal) => {
      signal = s;
      return new Promise((_resolve, reject) =>
        s.addEventListener("abort", () => reject(s.reason), { once: true }),
      );
    });
    const view = mount();
    await advance(1000);
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await advance(60000);
    expect(loadAll).toHaveBeenCalledTimes(1);
  });
  it.each(["online", "focus", "qwenpaw:auth-changed"])(
    "reconciles on the existing %s recovery signal",
    async (event) => {
      loadAll.mockResolvedValue({ loaded: 1, failed: [] });
      mount();
      await advance(1000);
      act(() => window.dispatchEvent(new Event(event)));
      await advance(1000);
      expect(loadAll).toHaveBeenCalledTimes(2);
    },
  );
  it("cancels a stalled connection and continues its mounted lifecycle", async () => {
    loadAll
      .mockImplementationOnce(
        (s: AbortSignal) =>
          new Promise((_resolve, reject) =>
            s.addEventListener("abort", () => reject(s.reason), { once: true }),
          ),
      )
      .mockResolvedValue({ loaded: 1, failed: [] });
    mount();
    await advance(1000);
    await advance(30000);
    await advance(1000);
    expect(state()).toEqual({ loading: false, error: null });
    expect(loadAll).toHaveBeenCalledTimes(2);
  });
});
