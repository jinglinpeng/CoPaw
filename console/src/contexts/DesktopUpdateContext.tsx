import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
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
  | "checking"
  | "downloading"
  | "installing"
  | "failed";

interface ContextValue {
  phase: UpdatePhase;
  hasUpdate: boolean;
  version: string;
  body: string;
  downloaded: number;
  total: number | null;
  throughputBps: number;
  error: UpdateError | null;
  startInstall: () => Promise<void>;
  retry: () => Promise<void>;
  dismissFailure: () => void;
}

const DesktopUpdateContext = createContext<ContextValue | null>(null);

const THROUGHPUT_WINDOW_MS = 5_000;

export function DesktopUpdateProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<UpdatePhase>("idle");
  const [hasUpdate, setHasUpdate] = useState(false);
  const [version, setVersion] = useState("");
  const [body, setBody] = useState("");
  const [downloaded, setDownloaded] = useState(0);
  const [total, setTotal] = useState<number | null>(null);
  const [throughputBps, setThroughputBps] = useState(0);
  const [error, setError] = useState<UpdateError | null>(null);

  const samplesRef = useRef<{ t: number; downloaded: number }[]>([]);

  // Probe on mount.
  useEffect(() => {
    if (!isDesktopApp()) return;
    let cancelled = false;
    checkDesktopUpdate()
      .then((info) => {
        if (cancelled || !info) return;
        setVersion(info.version);
        setBody(info.body?.trim() ?? "");
        setHasUpdate(true);
      })
      .catch((err) => {
        console.warn("[updates] desktop update check failed", err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleProgress = useCallback((p: UpdateProgress) => {
    const now = Date.now();
    samplesRef.current.push({ t: now, downloaded: p.downloaded });
    samplesRef.current = samplesRef.current.filter(
      (s) => now - s.t <= THROUGHPUT_WINDOW_MS,
    );
    const oldest = samplesRef.current[0];
    const dt = oldest ? (now - oldest.t) / 1000 : 0;
    const dBytes = oldest ? p.downloaded - oldest.downloaded : 0;
    setPhase("downloading");
    setDownloaded(p.downloaded);
    setTotal(p.total ?? null);
    setThroughputBps(dt > 0 ? Math.max(0, dBytes / dt) : 0);
  }, []);

  // Subscribe to Rust-side update:* events.
  useEffect(() => {
    if (!isDesktopApp()) return;
    let unlisten: (() => void) | null = null;
    let cancelled = false;
    onUpdateEvent({
      onCheckStart: () => setPhase("checking"),
      onDownloadProgress: handleProgress,
      onInstallStart: () => setPhase("installing"),
      onError: (err) => {
        setPhase("failed");
        setError(err);
      },
    }).then((u) => {
      if (cancelled) u();
      else unlisten = u;
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [handleProgress]);

  const startInstall = useCallback(async () => {
    samplesRef.current = [];
    setPhase("checking");
    setDownloaded(0);
    setTotal(null);
    setThroughputBps(0);
    setError(null);
    try {
      await installDesktopUpdate();
    } catch (err) {
      const message =
        typeof err === "string"
          ? err
          : err instanceof Error
          ? err.message
          : JSON.stringify(err);
      setPhase("failed");
      setError({ stage: "check", kind: "other", message });
    }
  }, []);

  const dismissFailure = useCallback(() => {
    setPhase("idle");
    setError(null);
  }, []);

  const value = useMemo<ContextValue>(
    () => ({
      phase,
      hasUpdate,
      version,
      body,
      downloaded,
      total,
      throughputBps,
      error,
      startInstall,
      retry: startInstall,
      dismissFailure,
    }),
    [
      phase,
      hasUpdate,
      version,
      body,
      downloaded,
      total,
      throughputBps,
      error,
      startInstall,
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
