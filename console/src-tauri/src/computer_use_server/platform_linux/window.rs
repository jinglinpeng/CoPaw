//! Window enumeration, resolution, identity, and the forbidden-window guard.

use serde_json::{json, Value};

use super::super::app_identity::app_id_from_path;
use super::super::state::{merge_app_list, WindowInfo};
use super::bridge::request;

/// Build a window from the platform process's record.
///
/// The identity is derived here from the owning process's executable, so an
/// approval keys on the same `process:` path a launch would produce.
pub(super) fn window_from_value(value: &Value) -> Option<WindowInfo> {
    let xid = value.get("xid").and_then(Value::as_i64)? as isize;
    let pid = value.get("pid").and_then(Value::as_i64).unwrap_or(0);
    let title = value
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if pid <= 0 || title.is_empty() {
        return None;
    }
    let executable = std::fs::read_link(format!("/proc/{pid}/exe")).ok()?;
    let display_name = executable
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or(&title)
        .to_string();
    Some(WindowInfo {
        hwnd: xid,
        app_id: app_id_from_path(&executable),
        display_name,
        title,
        class_name: value
            .get("wm_class")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
    })
}

fn enumerate_windows() -> Vec<WindowInfo> {
    let Ok(result) = request("windows", json!({})) else {
        return Vec::new();
    };
    result
        .as_array()
        .map(|windows| windows.iter().filter_map(window_from_value).collect())
        .unwrap_or_default()
}

pub(crate) fn list_windows(app: Option<&str>) -> Vec<Value> {
    enumerate_windows()
        .into_iter()
        .filter(|window| app.is_none_or(|value| window.matches_app(value)))
        .map(|window| window.to_json())
        .collect()
}

pub(crate) fn list_apps() -> Vec<Value> {
    merge_app_list(Vec::new(), enumerate_windows())
}

pub(crate) fn active_window() -> Option<WindowInfo> {
    let result = request("active", json!({})).ok()?;
    window_from_value(&result)
}

pub(crate) fn resolve_window(value: &str) -> Result<WindowInfo, (&'static str, String)> {
    let xid = value
        .parse::<i64>()
        .map_err(|_| ("invalid_request", "window_id is invalid.".to_string()))?;
    let result = request("window", json!({"xid": xid}))?;
    window_from_value(&result).ok_or((
        "window_not_found",
        "Target window was not found.".to_string(),
    ))
}

/// Credential prompts and QwenPaw itself are never automated.
pub(crate) fn is_forbidden(window: &WindowInfo) -> bool {
    let title = window.title.to_lowercase();
    let class_name = window.class_name.to_lowercase();
    let name = window.display_name.to_lowercase();
    let app = window.app_id.to_lowercase();
    let identity = [name.as_str(), class_name.as_str(), app.as_str()];
    identity.iter().any(|value| value.contains("qwenpaw"))
        || [
            "polkit",
            "pinentry",
            "gcr-prompter",
            "kwalletd",
            "ksecretd",
            "ssh-askpass",
        ]
        .iter()
        .any(|marker| identity.iter().any(|value| value.contains(marker)))
        || [
            "password",
            "credential",
            "keyring",
            "authentication required",
        ]
        .iter()
        .any(|marker| title.contains(marker))
}

pub(crate) fn close_window(window: &WindowInfo) -> Result<Value, (&'static str, String)> {
    request("close", json!({"xid": window.hwnd}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn window(title: &str, display_name: &str, app_id: &str) -> WindowInfo {
        WindowInfo {
            hwnd: 1,
            app_id: app_id.to_string(),
            display_name: display_name.to_string(),
            title: title.to_string(),
            class_name: String::new(),
        }
    }

    #[test]
    fn credential_prompts_and_qwenpaw_are_forbidden() {
        assert!(is_forbidden(&window(
            "Authentication Required",
            "polkit-gnome-authentication-agent-1",
            "process:/usr/libexec/polkit-gnome-authentication-agent-1",
        )));
        assert!(is_forbidden(&window("Unlock Keyring", "gcr-prompter", "process:/usr/libexec/gcr-prompter")));
        assert!(is_forbidden(&window("QwenPaw", "qwenpaw-desktop", "process:/usr/bin/qwenpaw-desktop")));
        assert!(!is_forbidden(&window("Untitled Document 1 - gedit", "gedit", "process:/usr/bin/gedit")));
    }
}
