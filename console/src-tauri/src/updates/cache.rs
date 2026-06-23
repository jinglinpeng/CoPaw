use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use super::signature::sha256_hex;

const CACHED_UPDATE_INSTALLER_DIR: &str = "cached-update-installer";
const UPDATE_META_FILE: &str = "update-meta.json";

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct UpdateMeta {
    pub(super) version: String,
    pub(super) installer_file: String,
    /// Base64 minisign signature string from the update manifest (same value
    /// `tauri-plugin-updater` verifies at download time). Re-verified before
    /// the cached installer is launched.
    #[serde(default)]
    pub(super) signature: String,
    /// Hex SHA-256 of the persisted installer bytes, for a fast corruption
    /// check before the (more expensive) signature verification.
    #[serde(default)]
    pub(super) sha256: String,
}

pub(super) fn cached_update_installer_dir(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_local_data_dir()
        .ok()
        .map(|p| p.join(CACHED_UPDATE_INSTALLER_DIR))
}

pub(super) fn has_cached_update_meta(cache_dir: &Path) -> bool {
    cache_dir.join(UPDATE_META_FILE).exists()
}

pub(super) fn read_cached_update_meta(cache_dir: &Path) -> Result<UpdateMeta, String> {
    let meta_str = std::fs::read_to_string(cache_dir.join(UPDATE_META_FILE))
        .map_err(|e| format!("no cached update found: {e}"))?;
    serde_json::from_str(&meta_str).map_err(|e| format!("invalid update meta: {e}"))
}

pub(super) fn remove_cached_update(cache_dir: &Path) {
    let _ = std::fs::remove_dir_all(cache_dir);
}

/// Persist the downloaded NSIS installer plus its metadata so a later "Restart
/// Now" only needs to verify and launch it.
pub(super) fn persist_cached_installer(
    app: &AppHandle,
    update: &tauri_plugin_updater::Update,
    bytes: &[u8],
) -> Result<(), String> {
    let cache_dir =
        cached_update_installer_dir(app).ok_or("cannot determine app data directory")?;
    if cache_dir.exists() {
        std::fs::remove_dir_all(&cache_dir).map_err(|e| e.to_string())?;
    }
    std::fs::create_dir_all(&cache_dir).map_err(|e| e.to_string())?;

    let exe_path = write_installer(bytes, &cache_dir, &update.version)?;
    let installer_file = exe_path
        .strip_prefix(&cache_dir)
        .ok()
        .and_then(|p| p.to_str())
        .ok_or("installer path is invalid")?
        .to_string();

    let meta = UpdateMeta {
        version: update.version.clone(),
        installer_file,
        signature: update.signature.clone(),
        sha256: sha256_hex(bytes),
    };
    let meta_json = serde_json::to_string_pretty(&meta).map_err(|e| e.to_string())?;
    std::fs::write(cache_dir.join(UPDATE_META_FILE), meta_json).map_err(|e| e.to_string())
}

fn write_installer(bytes: &[u8], dest_dir: &Path, version: &str) -> Result<PathBuf, String> {
    if !bytes.starts_with(b"MZ") {
        return Err("downloaded update is not a Windows installer executable".to_string());
    }

    let exe_name = format!("QwenPaw-Desktop_{version}_x64-setup.exe");
    let exe_path = dest_dir.join(&exe_name);
    std::fs::write(&exe_path, bytes).map_err(|e| e.to_string())?;
    Ok(exe_path)
}

pub(super) fn cached_installer_path(cache_dir: &Path, meta: &UpdateMeta) -> PathBuf {
    cache_dir.join(&meta.installer_file)
}
