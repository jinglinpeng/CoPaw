import {
  useState,
  useEffect,
  useRef,
  useCallback,
  type ReactNode,
} from "react";
import BackendLoadingPage from "./BackendLoadingPage";
import {
  getApiBaseUrl,
  initRuntimeApiBaseUrl,
  isTauriRuntime,
} from "../api/config";

const POLL_INTERVAL = 1000;
const POLL_TIMEOUT = 120;
const REQUEST_TIMEOUT = 5000;

interface Props {
  children: ReactNode;
}

export default function BackendReadyGate({ children }: Props) {
  const [status, setStatus] = useState<"checking" | "ready" | "timeout">(
    "checking",
  );
  const [shouldGate, setShouldGate] = useState(() => isTauriRuntime());
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const startPolling = useCallback((apiBaseUrl: string) => {
    setStatus("checking");
    setElapsed(0);

    const start = Date.now();

    const poll = async () => {
      try {
        const controller = new AbortController();
        const tid = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
        const res = await fetch(`${apiBaseUrl}/api/version`, {
          signal: controller.signal,
        });
        clearTimeout(tid);
        if (mountedRef.current && res.ok) {
          setStatus("ready");
          return;
        }
      } catch {
        // backend not ready yet
      }

      if (!mountedRef.current) return;
      const sec = Math.round((Date.now() - start) / 1000);
      setElapsed(sec);
      if (sec >= POLL_TIMEOUT) {
        setStatus("timeout");
        return;
      }
      timerRef.current = setTimeout(poll, POLL_INTERVAL);
    };

    poll();
  }, []);

  const retry = useCallback(() => {
    const apiBaseUrl = getApiBaseUrl();
    if (apiBaseUrl) startPolling(apiBaseUrl);
  }, [startPolling]);

  useEffect(() => {
    // Browser mode: pass through immediately.
    if (!shouldGate) return;

    mountedRef.current = true;
    initRuntimeApiBaseUrl()
      .then((apiBaseUrl) => {
        if (!mountedRef.current) return;
        if (apiBaseUrl) startPolling(apiBaseUrl);
        else setShouldGate(false);
      })
      .catch(() => {
        if (mountedRef.current) setStatus("timeout");
      });

    return () => {
      mountedRef.current = false;
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, [shouldGate, startPolling]);

  // Browser mode or backend ready.
  if (!shouldGate || status === "ready") {
    return <>{children}</>;
  }

  return (
    <BackendLoadingPage status={status} elapsed={elapsed} onRetry={retry} />
  );
}
