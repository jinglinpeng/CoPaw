import styles from "./Spinner.module.less";

interface Props {
  /** 0..1 progress; null for indeterminate (spinning) */
  progress: number | null;
  /** "default" green, "warn" orange, "error" red */
  tone?: "default" | "warn" | "error";
  /** Render an X mark instead of a spinner (for failure). */
  errorMark?: boolean;
}

const SIZE = 96;
const STROKE = 6;
const RADIUS = (SIZE - STROKE) / 2;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

export default function Spinner({
  progress,
  tone = "default",
  errorMark = false,
}: Props) {
  if (errorMark) {
    return (
      <div className={`${styles.spinner} ${styles.errorMark}`}>
        <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`}>
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            strokeWidth={STROKE}
            className={styles.errorRing}
          />
          <line
            x1={SIZE * 0.32}
            y1={SIZE * 0.32}
            x2={SIZE * 0.68}
            y2={SIZE * 0.68}
            strokeWidth={STROKE}
            strokeLinecap="round"
            className={styles.errorStroke}
          />
          <line
            x1={SIZE * 0.68}
            y1={SIZE * 0.32}
            x2={SIZE * 0.32}
            y2={SIZE * 0.68}
            strokeWidth={STROKE}
            strokeLinecap="round"
            className={styles.errorStroke}
          />
        </svg>
      </div>
    );
  }

  const determinate = progress !== null;
  const clamped = determinate ? Math.max(0, Math.min(1, progress)) : 0;
  const dashOffset = CIRCUMFERENCE * (1 - clamped);

  return (
    <div
      className={`${styles.spinner} ${determinate ? "" : styles.indeterminate}`}
      data-tone={tone}
    >
      <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`}>
        <circle
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={RADIUS}
          fill="none"
          strokeWidth={STROKE}
          className={styles.track}
        />
        <circle
          cx={SIZE / 2}
          cy={SIZE / 2}
          r={RADIUS}
          fill="none"
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={
            determinate
              ? `${CIRCUMFERENCE} ${CIRCUMFERENCE}`
              : `${CIRCUMFERENCE * 0.25} ${CIRCUMFERENCE}`
          }
          strokeDashoffset={determinate ? dashOffset : 0}
          className={styles.arc}
          transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
        />
      </svg>
    </div>
  );
}
