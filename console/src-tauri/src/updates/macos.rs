#[cfg(any(target_os = "macos", test))]
use std::path::Path;

pub(super) fn ensure_install_location() -> Result<(), String> {
    if let Some(reason) = current_install_location_issue() {
        return Err(reason.to_string());
    }
    Ok(())
}

pub(super) fn install_error_hint(err: &tauri_plugin_updater::Error) -> Option<String> {
    if !cfg!(target_os = "macos") {
        return None;
    }

    let message = err.to_string();
    if message.contains("read-only file system") || message.contains("os error: 30") {
        return Some(message);
    }
    None
}

#[cfg(target_os = "macos")]
fn current_install_location_issue() -> Option<&'static str> {
    let exe = std::env::current_exe().ok()?;
    install_location_issue(&exe)
}

#[cfg(not(target_os = "macos"))]
fn current_install_location_issue() -> Option<&'static str> {
    None
}

#[cfg(any(target_os = "macos", test))]
fn install_location_issue(exe: &Path) -> Option<&'static str> {
    let exe_text = exe.to_string_lossy();
    if exe_text.contains("/AppTranslocation/") {
        return Some("macOS App Translocation");
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_app_translocation() {
        let path = Path::new(
            "/private/var/folders/xx/AppTranslocation/123/d/QwenPaw.app/Contents/MacOS/QwenPaw",
        );
        assert_eq!(
            install_location_issue(path),
            Some("macOS App Translocation"),
        );
    }

    #[test]
    fn does_not_preflight_mounted_volume() {
        let path = Path::new("/Volumes/QwenPaw/QwenPaw.app/Contents/MacOS/QwenPaw");
        assert_eq!(install_location_issue(path), None);
    }

    #[test]
    fn allows_applications_folder() {
        let path = Path::new("/Applications/QwenPaw.app/Contents/MacOS/QwenPaw");
        assert_eq!(install_location_issue(path), None);
    }
}
