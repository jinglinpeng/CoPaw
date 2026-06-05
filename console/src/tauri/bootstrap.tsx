import { createRoot } from "react-dom/client";
import BackendReadyGate from "./BackendReadyGate";

// Lightweight theme detection (no full ThemeContext / antd dependency)
const isDark = (() => {
  try {
    const stored = localStorage.getItem("qwenpaw-theme");
    if (stored === "dark") return true;
    if (stored === "light") return false;
  } catch {
    /* ignore */
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
})();

if (isDark) {
  document.documentElement.classList.add("dark-mode");
}

createRoot(document.getElementById("root")!).render(
  <BackendReadyGate isDark={isDark}>{null}</BackendReadyGate>,
);
