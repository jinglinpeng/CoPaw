//! Tauri desktop entry point and plugin/command registration.

mod backend;
mod backend_download;
mod external_link;

use tauri::{Manager, RunEvent, WindowEvent};

/// Maximum time (ms) to wait for the frontend to call `show()` before
/// force-showing the window. Prevents a permanently hidden window if the
/// WebView fails to load.
const WINDOW_SHOW_SAFETY_TIMEOUT_MS: u64 = 5000;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
/// Build the desktop app, wire native plugins/commands, and stop the backend on exit.
pub fn run() {
    let build_result = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            backend_download::download_backend_file,
            backend::backend_port,
            backend::backend_startup_error,
            backend::restart_backend,
            external_link::open_external_link,
        ])
        .manage(backend::BackendState::default())
        .setup(|app| {
            // Safety timeout: force-show the main window if the frontend
            // fails to call `appWindow.show()` within the timeout.
            if let Some(window) = app.get_webview_window("main") {
                let win = window.clone();
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_millis(
                        WINDOW_SHOW_SAFETY_TIMEOUT_MS,
                    ));
                    if !win.is_visible().unwrap_or(true) {
                        log::warn!("[window] safety timeout reached, force-showing window");
                        let _ = win.show();
                    }
                });
            }
            backend::setup(app)
        })
        .on_window_event(|window, event| {
            // The app currently has a single "main" window, so closing it
            // is equivalent to quitting. If a multi-window mode is introduced,
            // make this window-count aware and keep the exit-event fallback.
            if matches!(event, WindowEvent::CloseRequested { .. }) {
                backend::stop(window.app_handle());
            }
        })
        .build(tauri::generate_context!());

    match build_result {
        Ok(app) => {
            app.run(|app_handle, event| {
                if let RunEvent::ExitRequested { .. } = event {
                    backend::stop(app_handle);
                }
            });
        }
        Err(err) => {
            eprintln!("[QwenPaw Desktop] Fatal startup error: {err}");
            std::process::exit(1);
        }
    }
}
