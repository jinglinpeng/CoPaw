//! Tauri commands for desktop auto-updates via tauri-plugin-updater.

use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter};
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
    app: AppHandle,
) -> Result<Option<DesktopUpdate>, String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?;

    Ok(update.map(|u| DesktopUpdate {
        version: u.version,
        body: u.body,
    }))
}

#[tauri::command]
pub(crate) fn install_desktop_update(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn(async move {
        run_install(app).await;
    });
    Ok(())
}

async fn run_install(app: AppHandle) {
    emit(&app, "update:check-start", &serde_json::json!({}));

    let update = match app
        .updater_builder()
        .on_before_exit({
            let app = app.clone();
            move || {
                backend::stop(&app);
                app.cleanup_before_exit();
            }
        })
        .build()
    {
        Ok(b) => b,
        Err(err) => return emit_error(&app, "check", &err),
    };

    let update = match update.check().await {
        Ok(Some(u)) => u,
        Ok(None) => return emit_error(&app, "check", &"no desktop update available"),
        Err(err) => return emit_error(&app, "check", &err),
    };

    let version = update.version.clone();
    log::info!("[updates] downloading desktop update version={version}");

    let mut last_emit: Option<Instant> = None;
    let mut downloaded: u64 = 0;

    let bytes = update
        .download(
            |chunk_len, content_len| {
                downloaded = downloaded.saturating_add(chunk_len as u64);
                let should_emit = last_emit
                    .map(|t| t.elapsed() >= Duration::from_millis(200))
                    .unwrap_or(true);
                if should_emit {
                    let _ = app.emit(
                        "update:download-progress",
                        serde_json::json!({
                            "downloaded": downloaded,
                            "total": content_len,
                        }),
                    );
                    last_emit = Some(Instant::now());
                }
            },
            || {
                log::info!("[updates] desktop update download complete");
            },
        )
        .await;

    let bytes = match bytes {
        Ok(b) => b,
        Err(err) => return emit_error(&app, "download", &err),
    };

    // Final progress frame (forces UI to land on 100%).
    let _ = app.emit(
        "update:download-progress",
        serde_json::json!({
            "downloaded": downloaded,
            "total": Some(downloaded),
        }),
    );

    log::info!("[updates] installing desktop update version={version}");
    emit(&app, "update:install-start", &serde_json::json!({}));

    if let Err(err) = update.install(bytes) {
        return emit_error(&app, "install", &err);
    }

    backend::stop(&app);
    app.restart();
}

fn emit<S: Serialize>(app: &AppHandle, name: &str, payload: &S) {
    if let Err(err) = app.emit(name, payload) {
        log::warn!("[updates] failed to emit {name}: {err}");
    }
}

fn emit_error(app: &AppHandle, stage: &'static str, err: &dyn std::fmt::Display) {
    let message = err.to_string();
    let kind = classify(&message);
    log::warn!("[updates] error stage={stage} kind={kind} message={message}");
    let _ = app.emit(
        "update:error",
        serde_json::json!({
            "stage": stage,
            "kind": kind,
            "message": message,
        }),
    );
}

fn classify(message: &str) -> &'static str {
    let s = message.to_lowercase();
    if s.contains("timed out")
        || s.contains("timeout")
        || s.contains("connection")
        || s.contains("dns")
        || s.contains("tls")
        || s.contains("network")
        || s.contains("unreachable")
        || s.contains("resolve")
    {
        "network"
    } else if s.contains("signature") || s.contains("verify") {
        "signature"
    } else {
        "other"
    }
}
