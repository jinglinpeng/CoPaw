//! Tauri commands for desktop auto-updates via tauri-plugin-updater.

mod cache;
mod events;
mod guard;
mod remote;
mod signature;
mod version;

use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_updater::UpdaterExt;

use crate::backend;

use cache::{
    cached_installer_path, cached_update_installer_dir, has_cached_update_meta,
    persist_cached_installer, read_cached_update_meta, remove_cached_update,
};
use events::{emit, emit_error, emit_updater_error};
use guard::begin_update;
use remote::check_and_download;
use signature::verify_cached_installer;
use version::version_lte;

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
    let guard = begin_update()?;
    tauri::async_runtime::spawn(async move {
        let _guard = guard;
        run_install(app).await;
    });
    Ok(())
}

async fn run_install(app: AppHandle) {
    let Some((update, bytes)) = check_and_download(&app).await else {
        return;
    };

    log::info!(
        "[updates] installing desktop update version={}",
        update.version
    );
    emit(&app, "update:install-start", &serde_json::json!({}));

    if let Err(err) = update.install(bytes) {
        return emit_updater_error(&app, "install", &err);
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

    let guard = begin_update()?;
    tauri::async_runtime::spawn(async move {
        let _guard = guard;
        run_background_download(app).await;
    });
    Ok(())
}

async fn run_background_download(app: AppHandle) {
    let Some((update, bytes)) = check_and_download(&app).await else {
        return;
    };

    if let Err(err) = persist_cached_installer(&app, &update, &bytes) {
        return emit_error(&app, "download", &err);
    }

    log::info!(
        "[updates] background download ready: version={}",
        update.version
    );
    emit(
        &app,
        "update:download-done",
        &serde_json::json!({ "version": update.version }),
    );
}

#[tauri::command]
pub(crate) fn install_downloaded_update(app: AppHandle) -> Result<(), String> {
    if !cfg!(windows) {
        return Err("cached installer updates are only supported on Windows".into());
    }

    let _guard = begin_update()?;

    let cache_dir =
        cached_update_installer_dir(&app).ok_or("cannot determine app data directory")?;
    let meta = read_cached_update_meta(&cache_dir)?;

    let exe_path = cached_installer_path(&cache_dir, &meta);
    if !exe_path.is_file() {
        return Err("installer exe not found - please download again".into());
    }

    // The cached installer lives in a user-writable directory, so "verified at
    // download time" is not enough: re-verify the on-disk bytes against the
    // configured updater public key right before launch (mirrors what
    // tauri-plugin-updater does before a foreground install). Any tampering or
    // corruption fails here and the stale cache is dropped.
    let bytes =
        std::fs::read(&exe_path).map_err(|e| format!("cannot read cached installer: {e}"))?;
    if let Err(err) = verify_cached_installer(&app, &meta, &bytes) {
        remove_cached_update(&cache_dir);
        return Err(err);
    }

    log::info!(
        "[updates] launching cached installer version={} exe={}",
        meta.version,
        exe_path.display()
    );
    backend::stop(&app);
    emit(&app, "update:install-start", &serde_json::json!({}));

    // Launch the NSIS installer in the same passive updater mode Tauri uses for
    // Windows installs, while skipping QwenPaw's optional PATH prompt.
    std::process::Command::new(&exe_path)
        .args(["/P", "/R", "/UPDATE", "/NO_QWENPAW_PATH"])
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

    let Some(cache_dir) = cached_update_installer_dir(&app) else {
        return Ok(None);
    };

    if !has_cached_update_meta(&cache_dir) {
        return Ok(None);
    }

    let Ok(meta) = read_cached_update_meta(&cache_dir) else {
        remove_cached_update(&cache_dir);
        return Ok(None);
    };

    // Compare with current app version. If cached version <= current, it's stale.
    let current_version = app.config().version.clone().unwrap_or_default();

    if version_lte(&meta.version, &current_version) {
        log::info!(
            "[updates] cleaning stale cached update: cached={} current={}",
            meta.version,
            current_version
        );
        remove_cached_update(&cache_dir);
        return Ok(None);
    }

    // Verify the installer exe exists.
    if !cached_installer_path(&cache_dir, &meta).is_file() {
        remove_cached_update(&cache_dir);
        return Ok(None);
    }

    Ok(Some(meta.version))
}
