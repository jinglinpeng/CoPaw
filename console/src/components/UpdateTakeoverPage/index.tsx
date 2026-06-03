import { Steps } from "antd";
import { Button } from "@agentscope-ai/design";
import { CopyOutlined, ClockCircleOutlined, ExclamationCircleOutlined } from "@ant-design/icons";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useDesktopUpdate } from "../../contexts/DesktopUpdateContext";
import Spinner from "./Spinner";
import styles from "./index.module.less";

/**
 * Wrap the normal app. When the desktop update flow is active in a non-Idle
 * non-Confirming phase, the takeover replaces the entire console UI.
 */
export function UpdateTakeoverGate({ children }: { children: ReactNode }) {
  const { phase } = useDesktopUpdate();
  const isActive =
    phase === "checking" ||
    phase === "downloading" ||
    phase === "installing" ||
    phase === "failed";
  return isActive ? <UpdateTakeoverPage /> : <>{children}</>;
}

const KEY_PREFIX = "sidebar.updateModal";

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatRate(bps: number): string {
  if (!Number.isFinite(bps) || bps <= 0) return "0 B/s";
  return `${formatBytes(bps)}/s`;
}

function formatEta(sec: number, t: (key: string, opts?: any) => string): string {
  if (!Number.isFinite(sec) || sec <= 0) return t(`${KEY_PREFIX}.etaUnknown`);
  if (sec < 1) return t(`${KEY_PREFIX}.etaDynamic`, { sec: 1 });
  if (sec >= 60) return t(`${KEY_PREFIX}.etaOverMinute`);
  return t(`${KEY_PREFIX}.etaDynamic`, { sec: Math.round(sec) });
}

function UpdateTakeoverPage() {
  const { t } = useTranslation();
  const update = useDesktopUpdate();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const stepIndex = useMemo(() => {
    switch (update.phase) {
      case "checking":
        return 0;
      case "downloading":
        return 1;
      case "installing":
        return 2;
      default:
        return 0;
    }
  }, [update.phase]);

  if (update.phase === "failed") {
    return <FailedView />;
  }

  const isDownloading = update.phase === "downloading";
  const isStalled = isDownloading && update.stalled;
  const total = update.total ?? null;
  const progress =
    isDownloading && total && total > 0
      ? Math.min(1, update.downloaded / total)
      : null;

  const tone = isStalled ? "warn" : "default";

  const title = (() => {
    if (update.phase === "checking") return t(`${KEY_PREFIX}.checking`);
    if (isStalled) return t(`${KEY_PREFIX}.stalledTitle`);
    if (update.phase === "downloading") return t(`${KEY_PREFIX}.downloading`);
    if (update.phase === "installing") return t(`${KEY_PREFIX}.installing`);
    return "";
  })();

  const subtitle = (() => {
    if (update.phase === "checking") return t(`${KEY_PREFIX}.checkingHint`);
    if (isStalled) return t(`${KEY_PREFIX}.stalledHint`);
    if (update.phase === "downloading" || update.phase === "installing") {
      return t(`${KEY_PREFIX}.downloadingTo`, { version: update.version });
    }
    return "";
  })();

  const progressLine = (() => {
    if (!isDownloading) return null;
    const done = formatBytes(update.downloaded);
    const totalLabel = total ? formatBytes(total) : "—";
    const rate = isStalled ? formatRate(0) : formatRate(update.throughputBps);
    return t(`${KEY_PREFIX}.downloadProgress`, {
      done,
      total: totalLabel,
      rate,
    });
  })();

  const etaLine = (() => {
    if (update.phase === "checking") return t(`${KEY_PREFIX}.etaCheckingHint`);
    if (update.phase === "installing") return t(`${KEY_PREFIX}.etaInstallingHint`);
    if (isDownloading)
      return isStalled
        ? t(`${KEY_PREFIX}.etaUnknown`)
        : formatEta(update.etaSec, t);
    return "";
  })();

  const handleCopy = () => {
    if (!update.error) return;
    navigator.clipboard.writeText(update.error.message).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className={styles.takeover}>
      <div className={styles.center}>
        <Spinner progress={progress} tone={tone} />

        <h1 className={styles.title}>{title}</h1>
        {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
        {(update.phase === "downloading" || update.phase === "installing") && (
          <p className={styles.willRestart}>
            {t(`${KEY_PREFIX}.willRestart`)}
          </p>
        )}

        <Steps
          size="small"
          current={stepIndex}
          className={styles.steps}
          items={[
            { title: t(`${KEY_PREFIX}.stepPrepare`) },
            { title: t(`${KEY_PREFIX}.stepDownloading`) },
            { title: t(`${KEY_PREFIX}.stepInstalling`) },
          ]}
        />

        {progressLine && <p className={styles.progressLine}>{progressLine}</p>}

        <div className={styles.hints}>
          <span className={styles.hint}>
            <ExclamationCircleOutlined /> {t(`${KEY_PREFIX}.dontClose`)}
          </span>
          {etaLine && (
            <span className={styles.hint}>
              <ClockCircleOutlined /> {etaLine}
            </span>
          )}
        </div>
      </div>
    </div>
  );

  function FailedView(): ReactNode {
    const errorKind = update.error?.kind ?? "other";
    const errorTitle = t(`${KEY_PREFIX}.failedTitle`);
    const errorMessage = t(`${KEY_PREFIX}.errors.${errorKind}`);

    return (
      <div className={styles.takeover}>
        <div className={styles.center}>
          <Spinner progress={null} errorMark />
          <h1 className={styles.title}>{errorTitle}</h1>
          <p className={styles.subtitle}>{errorMessage}</p>

          {update.error && (
            <div className={styles.detailsWrapper}>
              <button
                type="button"
                className={styles.detailsToggle}
                onClick={() => setDetailsOpen((v) => !v)}
              >
                {detailsOpen ? "▾" : "▸"} {t(`${KEY_PREFIX}.details`)}
              </button>
              {detailsOpen && (
                <div className={styles.detailsBox}>
                  <pre className={styles.detailsText}>
                    {`stage=${update.error.stage} kind=${update.error.kind}\n${update.error.message}`}
                  </pre>
                  <button
                    type="button"
                    className={styles.copyBtn}
                    onClick={handleCopy}
                  >
                    <CopyOutlined />{" "}
                    {copied ? "✓" : t(`${KEY_PREFIX}.copyDetails`)}
                  </button>
                </div>
              )}
            </div>
          )}

          <div className={styles.actions}>
            <Button onClick={update.dismissFailure}>
              {t(`${KEY_PREFIX}.back`)}
            </Button>
            <Button type="primary" onClick={update.retry}>
              {t(`${KEY_PREFIX}.retry`)}
            </Button>
          </div>
        </div>
      </div>
    );
  }
}
