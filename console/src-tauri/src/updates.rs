//! Tauri commands for desktop auto-updates via tauri-plugin-updater.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::UpdaterExt;

use crate::backend;

const CACHED_UPDATE_INSTALLER_DIR: &str = "cached-update-installer";

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UpdateMeta {
    version: String,
    ready_at: String,
    installer_file: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DesktopUpdate {
    version: String,
    body: Option<String>,
    supports_later_install: bool,
}

#[tauri::command]
pub(crate) async fn check_desktop_update(app: AppHandle) -> Result<Option<DesktopUpdate>, String> {
    let update = app
        .updater()
        .map_err(|e| e.to_string())?
        .check()
        .await
        .map_err(|e| e.to_string())?;

    Ok(update.map(|u| DesktopUpdate {
        version: u.version,
        body: u.body,
        supports_later_install: cfg!(windows),
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

    let bytes = match download_update(&app, &update).await {
        Ok(b) => b,
        Err(err) => return emit_error(&app, "download", &err),
    };

    log::info!("[updates] installing desktop update version={version}");
    emit(&app, "update:install-start", &serde_json::json!({}));

    if let Err(err) = update.install(bytes) {
        return emit_error(&app, "install", &err);
    }

    backend::stop(&app);
    app.restart();
}

#[tauri::command]
pub(crate) fn download_desktop_update(app: AppHandle) -> Result<(), String> {
    if !cfg!(windows) {
        return Err(
            "background update download is only supported for Windows installer packages".into(),
        );
    }

    tauri::async_runtime::spawn(async move {
        run_background_download(app).await;
    });
    Ok(())
}

async fn run_background_download(app: AppHandle) {
    emit(&app, "update:check-start", &serde_json::json!({}));

    let updater = match app
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

    let update = match updater.check().await {
        Ok(Some(u)) => u,
        Ok(None) => return emit_error(&app, "check", &"no desktop update available"),
        Err(err) => return emit_error(&app, "check", &err),
    };

    let version = update.version.clone();
    log::info!("[updates] background download starting version={version}");

    let bytes = match download_update(&app, &update).await {
        Ok(b) => b,
        Err(err) => return emit_error(&app, "download", &err),
    };

    // Persist the downloaded NSIS installer so "Restart Now" only needs to
    // launch it.
    let cache_dir = match cached_update_installer_dir(&app) {
        Some(d) => d,
        None => return emit_error(&app, "download", &"cannot determine app data directory"),
    };

    if cache_dir.exists() {
        if let Err(err) = std::fs::remove_dir_all(&cache_dir) {
            return emit_error(&app, "download", &err);
        }
    }
    if let Err(err) = std::fs::create_dir_all(&cache_dir) {
        return emit_error(&app, "download", &err);
    }

    let exe_path = match write_installer(&bytes, &cache_dir, &version) {
        Ok(path) => path,
        Err(err) => return emit_error(&app, "download", &err),
    };
    let installer_file = match exe_path
        .strip_prefix(&cache_dir)
        .ok()
        .and_then(|p| p.to_str())
    {
        Some(name) => name.to_string(),
        None => return emit_error(&app, "download", &"installer path is invalid"),
    };

    // Write metadata.
    let meta = UpdateMeta {
        version: version.clone(),
        ready_at: now_epoch_seconds(),
        installer_file,
    };
    let meta_path = cache_dir.join("update-meta.json");
    let meta_json = match serde_json::to_string_pretty(&meta) {
        Ok(json) => json,
        Err(err) => return emit_error(&app, "download", &err),
    };
    if let Err(err) = std::fs::write(&meta_path, meta_json) {
        return emit_error(&app, "download", &err);
    }

    log::info!("[updates] background download ready: version={version}");

    emit(
        &app,
        "update:download-done",
        &serde_json::json!({ "version": version }),
    );
}

#[tauri::command]
pub(crate) fn install_downloaded_update(app: AppHandle) -> Result<(), String> {
    if !cfg!(windows) {
        return Err("cached installer updates are only supported on Windows".into());
    }

    let cache_dir =
        cached_update_installer_dir(&app).ok_or("cannot determine app data directory")?;
    let meta_path = cache_dir.join("update-meta.json");

    let meta_str =
        std::fs::read_to_string(&meta_path).map_err(|e| format!("no cached update found: {e}"))?;
    let meta: UpdateMeta =
        serde_json::from_str(&meta_str).map_err(|e| format!("invalid update meta: {e}"))?;

    let exe_path = cached_installer_path(&cache_dir, &meta);
    if !exe_path.is_file() {
        return Err("installer exe not found - please download again".into());
    }

    log::info!(
        "[updates] launching installer version={} exe={}",
        meta.version,
        exe_path.display(),
    );

    backend::stop(&app);

    emit(&app, "update:install-start", &serde_json::json!({}));

    // Launch the NSIS installer in the same updater mode Tauri uses for
    // passive Windows installs, while skipping QwenPaw's optional PATH prompt.
    std::process::Command::new(&exe_path)
        .arg("/P")
        .arg("/R")
        .arg("/UPDATE")
        .arg("/NO_QWENPAW_PATH")
        .spawn()
        .map_err(|e| format!("failed to launch installer: {e}"))?;

    app.cleanup_before_exit();
    std::process::exit(0);
}

#[tauri::command]
pub(crate) async fn check_cached_update(app: AppHandle) -> Result<Option<String>, String> {
    if !cfg!(windows) {
        return Ok(None);
    }

    let cache_dir = match cached_update_installer_dir(&app) {
        Some(d) => d,
        None => return Ok(None),
    };

    let meta_path = cache_dir.join("update-meta.json");
    if !meta_path.exists() {
        return Ok(None);
    }

    let meta_str = match std::fs::read_to_string(&meta_path) {
        Ok(s) => s,
        Err(_) => {
            let _ = std::fs::remove_dir_all(&cache_dir);
            return Ok(None);
        }
    };

    let meta: UpdateMeta = match serde_json::from_str(&meta_str) {
        Ok(m) => m,
        Err(_) => {
            let _ = std::fs::remove_dir_all(&cache_dir);
            return Ok(None);
        }
    };

    // Compare with current app version. If cached version <= current, it's stale.
    let current_version = app.config().version.clone().unwrap_or_default();

    if version_lte(&meta.version, &current_version) {
        log::info!(
            "[updates] cleaning stale cached update: cached={} current={}",
            meta.version,
            current_version
        );
        let _ = std::fs::remove_dir_all(&cache_dir);
        return Ok(None);
    }

    // Verify the installer exe exists.
    if !cached_installer_path(&cache_dir, &meta).is_file() {
        let _ = std::fs::remove_dir_all(&cache_dir);
        return Ok(None);
    }

    Ok(Some(meta.version))
}

fn cached_update_installer_dir(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_local_data_dir()
        .ok()
        .map(|p| p.join(CACHED_UPDATE_INSTALLER_DIR))
}

fn write_installer(bytes: &[u8], dest_dir: &Path, version: &str) -> Result<PathBuf, String> {
    if bytes.len() < 2 || bytes[0] != b'M' || bytes[1] != b'Z' {
        return Err("downloaded update is not a Windows installer executable".to_string());
    }

    let exe_name = format!("QwenPaw-Desktop_{version}_x64-setup.exe");
    let exe_path = dest_dir.join(&exe_name);
    std::fs::write(&exe_path, bytes).map_err(|e| e.to_string())?;
    Ok(exe_path)
}

fn cached_installer_path(cache_dir: &Path, meta: &UpdateMeta) -> PathBuf {
    cache_dir.join(&meta.installer_file)
}

async fn download_update(
    app: &AppHandle,
    update: &tauri_plugin_updater::Update,
) -> Result<Vec<u8>, String> {
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
                    emit(
                        app,
                        "update:download-progress",
                        &serde_json::json!({
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
        .await
        .map_err(|err| err.to_string())?;

    // Final progress frame (forces UI to land on 100%).
    emit(
        app,
        "update:download-progress",
        &serde_json::json!({
            "downloaded": downloaded,
            "total": Some(downloaded),
        }),
    );

    Ok(bytes)
}

fn version_lte(a: &str, b: &str) -> bool {
    let parse = |v: &str| -> Vec<u64> {
        v.trim_start_matches('v')
            .split('.')
            .filter_map(|s| s.parse().ok())
            .collect()
    };
    let va = parse(a);
    let vb = parse(b);
    va <= vb
}

fn now_epoch_seconds() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("{now}")
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
