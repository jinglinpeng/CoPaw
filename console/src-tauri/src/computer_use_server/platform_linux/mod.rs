//! Linux Computer Use leaves.
//!
//! The shared layers (dispatch guards, observation state, approval, framing)
//! are the same code the Windows and macOS helpers run. This leaf does not call
//! desktop APIs itself: it forwards each platform operation to a small Python
//! process that owns the AT-SPI, Wnck, Gdk and XTEST work (see `bridge` and
//! `sidecar.py`). Only X11 sessions, including XWayland applications, are
//! supported.

mod actions;
mod bridge;
mod observe;
mod window;

pub(super) use actions::{
    click, desktop_locked, drag, input_sequence, invoke_element, last_input_age_ms, press_key,
    scroll, set_value, type_text,
};
pub(super) use observe::{observe_window, validate_observation};
pub(super) use window::{
    active_window, close_window, is_forbidden, list_apps, list_windows, resolve_window,
};

/// An accessibility element kept by the platform process: the observation it
/// was published in and its position in that observation's element list.
#[derive(Clone, Debug)]
pub(crate) struct LinuxElement {
    pub(crate) key: String,
    pub(crate) index: usize,
}

/// Methods that only enumerate windows do not need the accessibility bus.
pub(super) fn ensure_permissions(method: &str) -> Result<(), (&'static str, String)> {
    if matches!(method, "list_apps" | "list_windows" | "end_turn") {
        return Ok(());
    }
    bridge::request("ping", serde_json::json!({})).map(|_| ())
}

/// Exit with the process that started the helper, the way the macOS helper
/// watches its host, so a crashed host never leaves input synthesis running.
///
/// A helper started on purpose without a host (`QWENPAW_CU_DETACHED=1`, used
/// by the test scripts) keeps running until it is stopped.
pub(super) fn spawn_parent_death_watch() {
    if std::env::var_os("QWENPAW_CU_DETACHED").is_some_and(|value| value == "1") {
        return;
    }
    unsafe {
        if libc::getppid() <= 1 {
            return;
        }
        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
    }
}
