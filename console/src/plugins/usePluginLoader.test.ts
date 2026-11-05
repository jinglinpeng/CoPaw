// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setRuntimeApiBaseUrl } from "../api/config";
import { removePluginRuntime } from "./pluginRuntimeCleanup";
import { routeRegistry } from "./registry/store";
import {
  loadAllPlugins,
  PluginInitializationError,
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
    localStorage.clear();
    setRuntimeApiBaseUrl("");
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

  it("reports the explicit waiting contract to the lifecycle owner", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "FRONTEND_PLUGINS_STARTING",
            message: "initializing",
          },
        }),
        { status: 503 },
      ),
    );
    await expect(loadAllPlugins()).rejects.toMatchObject({
      retryable: true,
      waiting: true,
    });
  });

  it.each([404, 500, 503])(
    "does not disguise permanent HTTP %s as startup",
    async (status) => {
      const fetchMock = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(new Response("unavailable", { status }));
      await expect(loadAllPlugins()).rejects.toMatchObject({
        retryable: false,
        waiting: false,
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it("classifies transport failures separately from plugin exceptions", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    await expect(loadAllPlugins()).rejects.toBeInstanceOf(
      PluginInitializationError,
    );
    await expect(loadAllPlugins()).rejects.toMatchObject({ retryable: true });
  });

  it("reconciles and retries failed entries without reexecuting successful entries", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([plugin("notes", "app"), plugin("broken", "frontend")]),
      )
      .mockResolvedValueOnce(new Response("export default true"))
      .mockResolvedValueOnce(new Response("missing", { status: 404 }));
    expect((await loadAllPlugins()).failed).toHaveLength(1);
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([plugin("notes", "app"), plugin("broken", "frontend")]),
      )
      .mockResolvedValueOnce(new Response("export default true"));
    expect(await loadAllPlugins()).toEqual({ loaded: 2, failed: [] });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("does not retry permanent entry errors alongside a transport failure", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse([
          plugin("denied", "frontend"),
          plugin("offline", "frontend"),
        ]),
      )
      .mockResolvedValueOnce(new Response("denied", { status: 401 }))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const summary = await loadAllPlugins();
    expect(summary.failed).toHaveLength(2);
    expect(summary.retryable).not.toBe(true);
  });

  it("invalidates success after an auth or backend scope change", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock.mockImplementation(async (url) =>
      String(url).endsWith("/frontend_plugin")
        ? jsonResponse([plugin("notes", "frontend")])
        : new Response("export default true"),
    );
    await loadAllPlugins();
    await loadAllPlugins();
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    localStorage.setItem("qwenpaw_auth_token", "another-account");
    await loadAllPlugins();
    expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
    localStorage.removeItem("qwenpaw_auth_token");
  });

  it("reloads a changed version exactly once", async () => {
    let version = "1";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      String(url).endsWith("/frontend_plugin")
        ? jsonResponse([{ ...plugin("notes", "frontend"), version }])
        : new Response("export default true"),
    );
    await loadAllPlugins();
    version = "2";
    await loadAllPlugins();
    await loadAllPlugins();
    expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
  });

  it("invalidates entries removed from a later backend inventory", async () => {
    let present = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      String(url).endsWith("/frontend_plugin")
        ? jsonResponse(present ? [plugin("notes", "frontend")] : [])
        : new Response("export default true"),
    );
    await loadAllPlugins();
    present = false;
    await loadAllPlugins();
    present = true;
    await loadAllPlugins();
    expect(URL.createObjectURL).toHaveBeenCalledTimes(2);
  });

  it("cancels a shared-entry wait without aborting its other owner", async () => {
    let release!: (response: Response) => void;
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (url) =>
        String(url).endsWith("/frontend_plugin")
          ? jsonResponse([plugin("notes", "frontend")])
          : new Promise<Response>((resolve) => {
              release = resolve;
            }),
      );
    const owner = loadAllPlugins();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const controller = new AbortController();
    const waiter = loadAllPlugins(controller.signal);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    controller.abort();
    await expect(waiter).rejects.toMatchObject({ name: "AbortError" });
    release(new Response("export default true"));
    await expect(owner).resolves.toEqual({ loaded: 1, failed: [] });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("aborts an entry fetch before executing its script", async () => {
    const controller = new AbortController();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "frontend")]))
      .mockImplementation(
        (_url, options) =>
          new Promise((_resolve, reject) => {
            options?.signal?.addEventListener(
              "abort",
              () => reject(new DOMException("Cancelled", "AbortError")),
              { once: true },
            );
            controller.abort();
          }),
      );
    await expect(loadAllPlugins(controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("invalidates success when the backend changes or runtime is removed", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      String(url).endsWith("/frontend_plugin")
        ? jsonResponse([plugin("notes", "frontend")])
        : new Response("export default true"),
    );
    await loadAllPlugins();
    setRuntimeApiBaseUrl("http://another-runtime");
    await loadAllPlugins();
    removePluginRuntime("notes");
    await loadAllPlugins();
    expect(URL.createObjectURL).toHaveBeenCalledTimes(3);
    setRuntimeApiBaseUrl("");
  });

  it("deduplicates concurrent startup reconciliations", async () => {
    let release: ((value: Response) => void) | undefined;
    let entries = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      if (String(url).endsWith("/frontend_plugin"))
        return jsonResponse([plugin("notes", "frontend")]);
      entries++;
      return new Promise<Response>((resolve) => {
        release = resolve;
      });
    });
    const first = loadAllPlugins();
    const second = loadAllPlugins();
    await vi.waitFor(() => expect(entries).toBe(1));
    release!(new Response("export default true"));
    await Promise.all([first, second]);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("does not classify a plugin-code TypeError as a network retry", async () => {
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          'throw new TypeError("plugin bug")',
        )}`,
    );
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse([plugin("broken", "frontend")]))
      .mockResolvedValueOnce(new Response('throw new TypeError("plugin bug")'));
    const result = await loadAllPlugins();
    expect(result.failed[0]).toContain("plugin bug");
    expect(result.retryable).not.toBe(true);
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
