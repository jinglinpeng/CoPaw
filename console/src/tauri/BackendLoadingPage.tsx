/**
 * BackendLoadingPage — lightweight version (no antd, no full i18n).
 *
 * Uses a pure-CSS circular progress indicator and inline translations
 * to keep the bootstrap bundle as small as possible.
 */
import { type CSSProperties } from "react";
import { type BackendReadyStatus } from "./useBackendReadyPolling";
import styles from "./BackendLoadingPage.module.less";

const BRAND_COLOR = "#ff7f16";
const ERROR_COLOR = "#ff4d4f";

// Inline translations (only the keys used by this loading page)
const TRANSLATIONS: Record<string, Record<string, string>> = {
  en: {
    starting: "Starting backend...",
    checking: "Connecting to backend...",
    error: "Backend failed to start.",
    timeout: "Backend failed to start within {{seconds}} seconds.",
    errorHint:
      "The backend process could not be launched. Check application logs for details.",
    timeoutHint:
      "Backend failed to start. Please retry, or check application logs for details.",
    errorDetails: "Show error details",
    retry: "Retry",
  },
  zh: {
    starting: "正在启动后端...",
    checking: "正在连接后端...",
    error: "后端启动失败。",
    timeout: "后端在 {{seconds}} 秒内未能启动。",
    errorHint: "后端进程无法启动，请查看应用日志了解详情。",
    timeoutHint: "后端启动失败，请重试或查看应用日志。",
    errorDetails: "显示错误详情",
    retry: "重试",
  },
  ja: {
    starting: "バックエンドを起動中...",
    checking: "バックエンドに接続中...",
    error: "バックエンドの起動に失敗しました。",
    timeout: "バックエンドが {{seconds}} 秒以内に起動できませんでした。",
    errorHint:
      "バックエンドプロセスを起動できませんでした。アプリケーションログを確認してください。",
    timeoutHint:
      "バックエンドの起動に失敗しました。再試行するか、ログを確認してください。",
    errorDetails: "エラー詳細を表示",
    retry: "再試行",
  },
  ru: {
    starting: "Запуск бэкенда...",
    checking: "Подключение к бэкенду...",
    error: "Бэкенд не удалось запустить.",
    timeout: "Бэкенд не удалось запустить за {{seconds}} секунд.",
    errorHint:
      "Процесс бэкенда не удалось запустить. Проверьте журналы приложения.",
    timeoutHint:
      "Не удалось запустить бэкенд. Пожалуйста, повторите попытку или проверьте журналы.",
    errorDetails: "Показать подробности ошибки",
    retry: "Повторить",
  },
  id: {
    starting: "Memulai backend...",
    checking: "Menghubungkan ke backend...",
    error: "Backend gagal dimulai.",
    timeout: "Backend gagal dimulai dalam {{seconds}} detik.",
    errorHint: "Proses backend tidak dapat diluncurkan. Periksa log aplikasi.",
    timeoutHint: "Backend gagal dimulai. Silakan coba lagi, atau periksa log.",
    errorDetails: "Tampilkan detail error",
    retry: "Coba Lagi",
  },
  "pt-BR": {
    starting: "Iniciando backend...",
    checking: "Conectando ao backend...",
    error: "O backend falhou ao iniciar.",
    timeout: "O backend não iniciou em {{seconds}} segundos.",
    errorHint:
      "O processo do backend não pôde ser iniciado. Verifique os logs do aplicativo.",
    timeoutHint:
      "O backend falhou ao iniciar. Tente novamente ou verifique os logs.",
    errorDetails: "Mostrar detalhes do erro",
    retry: "Tentar novamente",
  },
};

function detectLocale(): string {
  try {
    const stored = localStorage.getItem("qwenpaw-language");
    if (stored && TRANSLATIONS[stored]) return stored;
  } catch {
    /* ignore */
  }
  const nav = navigator.language || "";
  if (nav.startsWith("zh")) return "zh";
  if (nav.startsWith("ja")) return "ja";
  if (nav.startsWith("ru")) return "ru";
  if (nav.startsWith("id")) return "id";
  if (nav.startsWith("pt")) return "pt-BR";
  return "en";
}

const locale = detectLocale();
const t = (key: string, vars?: Record<string, string | number>): string => {
  let text =
    TRANSLATIONS[locale]?.[key] || TRANSLATIONS["en"][key] || key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      text = text.replace(`{{${k}}}`, String(v));
    }
  }
  return text;
};

interface BackendLoadingPageProps {
  status: BackendReadyStatus;
  elapsed: number;
  totalSec: number;
  errorMessage?: string;
  onRetry?: () => void;
  isDark?: boolean;
}

export default function BackendLoadingPage({
  status,
  elapsed,
  totalSec,
  errorMessage,
  onRetry,
  isDark,
}: BackendLoadingPageProps) {
  const hasFailed = status === "timeout" || status === "error";
  const statusText =
    status === "error"
      ? t("error")
      : status === "checking"
      ? elapsed === 0
        ? t("starting")
        : t("checking")
      : t("timeout", { seconds: elapsed });

  const percent = Math.min(Math.round((elapsed / totalSec) * 100), 100);
  const strokeColor = hasFailed ? ERROR_COLOR : BRAND_COLOR;

  // SVG circular progress
  const size = 160;
  const strokeWidth = 8;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  // Gap at bottom (like antd dashboard mode): leave 25% gap
  const arcLength = circumference * 0.75;
  const dashOffset = arcLength - (arcLength * percent) / 100;

  const style = {
    "--qwenpaw-brand-color": BRAND_COLOR,
    "--qwenpaw-error-color": ERROR_COLOR,
  } as CSSProperties;

  return (
    <div
      className={`${styles.page} ${
        isDark ? styles.pageDark : styles.pageLight
      }`}
      style={style}
    >
      <div className={styles.card}>
        <img src="/qwenpaw.png" alt="QwenPaw" className={styles.logo} />

        {/* Pure CSS/SVG circular progress */}
        <div style={{ position: "relative", width: size, height: size, margin: "0 auto" }}>
          <svg
            width={size}
            height={size}
            viewBox={`0 0 ${size} ${size}`}
            style={{ transform: "rotate(135deg)" }}
          >
            {/* Background track */}
            <circle
              cx={size / 2}
              cy={size / 2}
              r={radius}
              fill="none"
              stroke={isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.04)"}
              strokeWidth={strokeWidth}
              strokeDasharray={`${arcLength} ${circumference}`}
              strokeLinecap="round"
            />
            {/* Progress arc */}
            <circle
              cx={size / 2}
              cy={size / 2}
              r={radius}
              fill="none"
              stroke={strokeColor}
              strokeWidth={strokeWidth}
              strokeDasharray={`${arcLength} ${circumference}`}
              strokeDashoffset={dashOffset}
              strokeLinecap="round"
              style={{ transition: "stroke-dashoffset 0.3s ease" }}
            />
          </svg>
          {/* Center label */}
          <div
            className={styles.progressLabel}
            style={{
              position: "absolute",
              top: "50%",
              left: "50%",
              transform: "translate(-50%, -50%)",
              fontSize: 20,
              fontWeight: 500,
            }}
          >
            {`${elapsed}s`}
          </div>
        </div>

        <p
          className={`${styles.statusText} ${
            hasFailed ? styles.failedText : ""
          }`}
        >
          {statusText}
        </p>

        {hasFailed && (
          <>
            <p className={styles.hint}>
              {status === "error" ? t("errorHint") : t("timeoutHint")}
            </p>
            {errorMessage && (
              <details className={styles.details}>
                <summary className={styles.summary}>
                  {t("errorDetails")}
                </summary>
                <pre className={styles.errorDetails}>{errorMessage}</pre>
              </details>
            )}
            <button
              className={styles.retryButton}
              onClick={onRetry}
              type="button"
            >
              {t("retry")}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
