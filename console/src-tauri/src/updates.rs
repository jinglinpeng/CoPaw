//! Desktop update commands backed by Tauri's updater plugin.

use serde::Serialize;
use tauri_plugin_updater::UpdaterExt;

use crate::backend;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DesktopUpdate {
    version: String,
    body: Option<String>,
}

#[tauri::command]
pub(crate) async fn check_desktop_update(
    app: tauri::AppHandle,
) -> Result<Option<DesktopUpdate>, String> {
    let update = app
        .updater()
        .map_err(updater_error)?
        .check()
        .await
        .map_err(updater_error)?;

    Ok(update.map(|u| DesktopUpdate {
        version: u.version,
        body: u.body,
    }))
}

#[tauri::command]
pub(crate) async fn install_desktop_update(app: tauri::AppHandle) -> Result<(), String> {
    // `on_before_exit` covers platforms where the updater itself triggers exit
    // (Windows passive install). For macOS the call returns with the new bundle
    // in place and the app keeps running until `app.restart()` below, so we
    // stop the backend explicitly there.
    let update = app
        .updater_builder()
        .on_before_exit({
            let app = app.clone();
            move || {
                backend::stop(&app);
                app.cleanup_before_exit();
            }
        })
        .build()
        .map_err(updater_error)?
        .check()
        .await
        .map_err(updater_error)?
        .ok_or_else(|| "no desktop update available".to_string())?;

    let version = update.version.clone();
    log::info!("[updates] downloading desktop update version={version}");
    let bytes = update
        .download(
            |chunk_len, content_len| {
                log::debug!(
                    "[updates] downloaded chunk bytes={} total={}",
                    chunk_len,
                    content_len
                        .map(|len| len.to_string())
                        .unwrap_or_else(|| "unknown".to_string())
                );
            },
            || {
                log::info!("[updates] desktop update download complete");
            },
        )
        .await
        .map_err(updater_error)?;

    log::info!("[updates] installing desktop update version={version}");
    update.install(bytes).map_err(updater_error)?;
    backend::stop(&app);
    app.restart();
}

fn updater_error(err: impl std::fmt::Display) -> String {
    err.to_string()
}
