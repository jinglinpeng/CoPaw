// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routeRegistry } from "./registry/store";
import {
  loadAllPlugins,
  loadPawApp,
  reloadPawApp,
  resetPawAppLoaderForTests,
} from "./usePluginLoader";

const originalCreateObjectUrl = URL.createObjectURL;
const originalRevokeObjectUrl = URL.revokeObjectURL;

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function plugin(id: string, type: string) {
  return {
    id,
    name: id,
    plugin_type: type,
    frontend_entry: "dist/index.js",
  };
}

describe("frontend plugin loader", () => {
  beforeEach(() => {
    resetPawAppLoaderForTests();
    routeRegistry.__resetForTests();
    vi.restoreAllMocks();
    URL.createObjectURL = vi.fn(
      () => `data:text/javascript,${encodeURIComponent("export default true")}`,
    );
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.useRealTimers();
    URL.createObjectURL = originalCreateObjectUrl;
    URL.revokeObjectURL = originalRevokeObjectUrl;
    delete (globalThis as typeof globalThis & { __registerNotes?: () => void })
      .__registerNotes;
  });

  it("loads every installed frontend plugin during startup", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([
          plugin("global-tools", "frontend"),
          plugin("notes", "app"),
        ]),
      )
      .mockImplementation(async () => new Response("export default true"));

    await expect(loadAllPlugins()).resolves.toEqual({
      loaded: 2,
      failed: [],
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([404, 503])(
    "recovers from initial HTTP %s before executing plugin code once",
    async (status) => {
      vi.useFakeTimers();
      const fetchMock = vi.spyOn(globalThis, "fetch");
      fetchMock
        .mockResolvedValueOnce(new Response("not ready", { status }))
        .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
        .mockResolvedValueOnce(new Response("export default true"));
      const result = loadAllPlugins();
      await vi.advanceTimersByTimeAsync(1000);
      await expect(result).resolves.toEqual({ loaded: 1, failed: [] });
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    },
  );

  it("recovers after a connection failure", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(jsonResponse([]));
    const result = loadAllPlugins();
    await vi.advanceTimersByTimeAsync(1000);
    await expect(result).resolves.toEqual({ loaded: 0, failed: [] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("reports a persistent list failure after bounded retries", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(
        async () => new Response("not ready", { status: 503 }),
      );
    const result = loadAllPlugins().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(15000);
    expect(await result).toEqual(
      new Error("Failed to list frontend plugins (503)"),
    );
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it.each([401, 403])(
    "does not retry authentication failure %s",
    async (status) => {
      const fetchMock = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(new Response("denied", { status }));
      await expect(loadAllPlugins()).rejects.toThrow(`(${status})`);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it("does not rerun successful plugins when another entry fails", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([plugin("notes", "app"), plugin("broken", "frontend")]),
      )
      .mockResolvedValueOnce(new Response("export default true"))
      .mockResolvedValueOnce(new Response("not ready", { status: 503 }));
    const result = await loadAllPlugins();
    expect(result.loaded).toBe(1);
    expect(result.failed).toHaveLength(1);
    expect(result.failed[0]).toContain("broken: Error: HTTP 503");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("loads a newly installed PawApp and exposes its route immediately", async () => {
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerNotes?: () => void;
    };
    runtimeGlobal.__registerNotes = () => {
      routeRegistry.add("notes", {
        id: "notes.page",
        path: "/apps/notes",
        component: () => null,
      });
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerNotes()",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(new Response("globalThis.__registerNotes()"));

    await expect(loadPawApp("notes")).resolves.toBeUndefined();
    expect(routeRegistry.snapshot()).toMatchObject([
      { id: "notes.page", path: "/apps/notes", source: "notes" },
    ]);
  });

  it("deduplicates concurrent loads and allows retry after failure", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(new Response("missing", { status: 503 }));

    const first = loadPawApp("notes");
    expect(loadPawApp("notes")).toBe(first);
    await expect(first).rejects.toThrow("HTTP 503");

    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockImplementationOnce(async () => {
        routeRegistry.add("notes", {
          id: "notes.page",
          path: "/apps/notes",
          component: () => null,
        });
        return new Response("export default true");
      });

    await expect(loadPawApp("notes")).resolves.toBeUndefined();
  });

  it("force reloads an already registered PawApp route", async () => {
    const oldComponent = () => null;
    const newComponent = () => null;
    routeRegistry.add("notes", {
      id: "notes.page",
      path: "/apps/notes",
      component: oldComponent,
    });
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerUpdatedNotes?: () => void;
    };
    runtimeGlobal.__registerUpdatedNotes = () => {
      routeRegistry.add("notes", {
        id: "notes.page",
        path: "/apps/notes",
        component: newComponent,
      });
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerUpdatedNotes()",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(new Response("updated"));

    await expect(reloadPawApp("notes")).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(routeRegistry.snapshot()[0].Component).toBe(newComponent);
    expect(routeRegistry.snapshot()[0].Component).not.toBe(oldComponent);
  });
});
