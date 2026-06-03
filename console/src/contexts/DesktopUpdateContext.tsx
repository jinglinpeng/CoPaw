import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  checkDesktopUpdate,
  installDesktopUpdate,
  onUpdateEvent,
  type UpdateError,
  type UpdateProgress,
} from "../tauri/desktopUpdate";
import { isDesktopApp } from "../tauri/backendRuntime";

export type UpdatePhase =
  | "idle"
  | "confirming"
  | "checking"
  | "downloading"
  | "installing"
  | "failed";

interface UpdateState {
  phase: UpdatePhase;
  /** Target version offered by tauri-plugin-updater. Empty string when none. */
  version: string;
  /** Release notes / body from the manifest. */
  body: string;
  /** Bytes downloaded so far during the active download. */
  downloaded: number;
  /** Total bytes (null until first chunk reports a Content-Length). */
  total: number | null;
  /** Rolling 5s throughput in bytes/sec. */
  throughputBps: number;
  /** Estimated remaining seconds. Infinity when unknown. */
  etaSec: number;
  /** True after >=30s without a fresh progress event during downloading. */
  stalled: boolean;
  /** Last error, populated when phase === "failed". */
  error: UpdateError | null;
}

interface UpdateActions {
  /** Initial Header click — opens the confirmation modal. */
  openConfirming: () => void;
  /** Close the confirming modal without starting. */
  closeConfirming: () => void;
  /** Confirm + start the install task. Transitions through Checking → Downloading → Installing. */
  startInstall: () => Promise<void>;
  /** From Failed: retry the whole flow. */
  retry: () => Promise<void>;
  /** From Failed: bail out, returning to Idle (closes takeover). */
  dismissFailure: () => void;
}

interface ContextValue extends UpdateState, UpdateActions {
  /** True when there's a known available update. */
  hasUpdate: boolean;
}

const DesktopUpdateContext = createContext<ContextValue | null>(null);

const STALLED_THRESHOLD_MS = 30_000;
const THROUGHPUT_WINDOW_MS = 5_000;

type Action =
  | { type: "set-available"; version: string; body: string }
  | { type: "open-confirming" }
  | { type: "close-confirming" }
  | { type: "checking" }
  | {
      type: "progress";
      downloaded: number;
      total: number | null;
      throughputBps: number;
      etaSec: number;
    }
  | { type: "stalled"; stalled: boolean }
  | { type: "installing" }
  | { type: "installed" }
  | { type: "error"; error: UpdateError }
  | { type: "reset-to-idle" };

const initialState: UpdateState = {
  phase: "idle",
  version: "",
  body: "",
  downloaded: 0,
  total: null,
  throughputBps: 0,
  etaSec: Infinity,
  stalled: false,
  error: null,
};

function reducer(state: UpdateState, action: Action): UpdateState {
  switch (action.type) {
    case "set-available":
      return { ...state, version: action.version, body: action.body };
    case "open-confirming":
      return { ...state, phase: "confirming" };
    case "close-confirming":
      return { ...state, phase: "idle" };
    case "checking":
      return {
        ...state,
        phase: "checking",
        downloaded: 0,
        total: null,
        throughputBps: 0,
        etaSec: Infinity,
        stalled: false,
        error: null,
      };
    case "progress":
      return {
        ...state,
        phase: "downloading",
        downloaded: action.downloaded,
        total: action.total,
        throughputBps: action.throughputBps,
        etaSec: action.etaSec,
      };
    case "stalled":
      return { ...state, stalled: action.stalled };
    case "installing":
      return { ...state, phase: "installing", stalled: false };
    case "installed":
      // Final state. Tauri will exit/restart the process; UI is short-lived.
      return state;
    case "error":
      return { ...state, phase: "failed", error: action.error };
    case "reset-to-idle":
      return { ...initialState, version: state.version, body: state.body };
    default:
      return state;
  }
}

interface ProviderProps {
  children: ReactNode;
}

export function DesktopUpdateProvider({ children }: ProviderProps) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [hasUpdate, setHasUpdate] = useState(false);

  const samplesRef = useRef<{ t: number; downloaded: number }[]>([]);
  const lastProgressAtRef = useRef<number>(Date.now());

  // ── 1. Probe on mount: ask tauri-plugin-updater whether there's a new version.
  useEffect(() => {
    if (!isDesktopApp()) return;
    let cancelled = false;
    checkDesktopUpdate()
      .then((info) => {
        if (cancelled || !info) return;
        dispatch({
          type: "set-available",
          version: info.version,
          body: info.body?.trim() ?? "",
        });
        setHasUpdate(true);
      })
      .catch((err) => {
        // Soft fail — log to console for diagnostics, don't block the UI.
        console.warn("[updates] desktop update check failed", err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ── 2. Subscribe to Rust-side events so the takeover can render progress.
  useEffect(() => {
    if (!isDesktopApp()) return;
    let unlisten: (() => void) | null = null;
    let cancelled = false;
    onUpdateEvent({
      onCheckStart: () => dispatch({ type: "checking" }),
      onDownloadProgress: (progress) => handleProgress(progress),
      onInstallStart: () => dispatch({ type: "installing" }),
      onInstallDone: () => dispatch({ type: "installed" }),
      onError: (error) => dispatch({ type: "error", error }),
    }).then((u) => {
      if (cancelled) {
        u();
      } else {
        unlisten = u;
      }
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 3. Stalled watchdog: tick every 1s, flip when no progress for 30s.
  useEffect(() => {
    if (state.phase !== "downloading") return;
    const id = window.setInterval(() => {
      const since = Date.now() - lastProgressAtRef.current;
      dispatch({ type: "stalled", stalled: since > STALLED_THRESHOLD_MS });
    }, 1000);
    return () => window.clearInterval(id);
  }, [state.phase]);

  const handleProgress = useCallback((progress: UpdateProgress) => {
    const now = Date.now();
    lastProgressAtRef.current = now;

    samplesRef.current.push({ t: now, downloaded: progress.downloaded });
    samplesRef.current = samplesRef.current.filter(
      (s) => now - s.t <= THROUGHPUT_WINDOW_MS,
    );
    const oldest = samplesRef.current[0];
    const dt = oldest ? (now - oldest.t) / 1000 : 0;
    const dBytes = oldest ? progress.downloaded - oldest.downloaded : 0;
    const throughputBps = dt > 0 ? Math.max(0, dBytes / dt) : 0;
    const total = progress.total ?? null;
    const etaSec =
      throughputBps > 0 && total !== null && total > progress.downloaded
        ? (total - progress.downloaded) / throughputBps
        : Infinity;

    dispatch({
      type: "progress",
      downloaded: progress.downloaded,
      total,
      throughputBps,
      etaSec,
    });
  }, []);

  const openConfirming = useCallback(() => {
    if (!hasUpdate) return;
    dispatch({ type: "open-confirming" });
  }, [hasUpdate]);

  const closeConfirming = useCallback(() => {
    dispatch({ type: "close-confirming" });
  }, []);

  const startInstall = useCallback(async () => {
    samplesRef.current = [];
    lastProgressAtRef.current = Date.now();
    dispatch({ type: "checking" });
    try {
      await installDesktopUpdate();
    } catch (err) {
      const message =
        typeof err === "string"
          ? err
          : err instanceof Error
          ? err.message
          : JSON.stringify(err);
      dispatch({
        type: "error",
        error: {
          stage: "check",
          kind: "other",
          message,
        },
      });
    }
  }, []);

  const retry = useCallback(async () => {
    await startInstall();
  }, [startInstall]);

  const dismissFailure = useCallback(() => {
    dispatch({ type: "reset-to-idle" });
  }, []);

  const value = useMemo<ContextValue>(
    () => ({
      ...state,
      hasUpdate,
      openConfirming,
      closeConfirming,
      startInstall,
      retry,
      dismissFailure,
    }),
    [
      state,
      hasUpdate,
      openConfirming,
      closeConfirming,
      startInstall,
      retry,
      dismissFailure,
    ],
  );

  return (
    <DesktopUpdateContext.Provider value={value}>
      {children}
    </DesktopUpdateContext.Provider>
  );
}

export function useDesktopUpdate(): ContextValue {
  const ctx = useContext(DesktopUpdateContext);
  if (!ctx) {
    throw new Error(
      "useDesktopUpdate must be used inside <DesktopUpdateProvider>",
    );
  }
  return ctx;
}
