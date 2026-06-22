//! Tauri commands for desktop auto-updates via tauri-plugin-updater.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use base64::Engine;
use minisign_verify::{PublicKey, Signature};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::UpdaterExt;

use crate::backend;

const CACHED_UPDATE_INSTALLER_DIR: &str = "cached-update-installer";

/// Guards against concurrent update operations (check/download/install). Each
/// of the public commands acquires this before spawning work and releases it
/// when the work finishes, so repeated clicks can't race on the cache dir.
static UPDATE_IN_FLIGHT: AtomicBool = AtomicBool::new(false);

/// RAII token: holding it means an update operation is in flight; dropping it
/// (including on early returns / errors) clears the flag.
struct InFlightGuard;

impl InFlightGuard {
    fn try_acquire() -> Option<Self> {
        UPDATE_IN_FLIGHT
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .ok()
            .map(|_| InFlightGuard)
    }
}

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        UPDATE_IN_FLIGHT.store(false, Ordering::Release);
    }
}

fn begin_update() -> Result<InFlightGuard, String> {
    InFlightGuard::try_acquire()
        .ok_or_else(|| "an update operation is already in progress".to_string())
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UpdateMeta {
    version: String,
    installer_file: String,
    /// Base64 minisign signature string from the update manifest (same value
    /// `tauri-plugin-updater` verifies at download time). Re-verified before
    /// the cached installer is launched.
    #[serde(default)]
    signature: String,
    /// Hex SHA-256 of the persisted installer bytes, for a fast corruption
    /// check before the (more expensive) signature verification.
    #[serde(default)]
    sha256: String,
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
    let guard = begin_update()?;
    tauri::async_runtime::spawn(async move {
        let _guard = guard;
        run_install(app).await;
    });
    Ok(())
}

async fn run_install(app: AppHandle) {
    emit(&app, "update:check-start", &serde_json::json!({}));

    let update = match check_installable_update(&app).await {
        Ok(Some(update)) => update,
        Ok(None) => return emit_error(&app, "check", &"no desktop update available"),
        Err(err) => return emit_updater_error(&app, "check", &err),
    };

    let version = update.version.clone();
    log::info!("[updates] downloading desktop update version={version}");

    let bytes = match download_update(&app, &update).await {
        Ok(b) => b,
        Err(err) => return emit_updater_error(&app, "download", &err),
    };

    log::info!("[updates] installing desktop update version={version}");
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
    emit(&app, "update:check-start", &serde_json::json!({}));

    let update = match check_installable_update(&app).await {
        Ok(Some(update)) => update,
        Ok(None) => return emit_error(&app, "check", &"no desktop update available"),
        Err(err) => return emit_updater_error(&app, "check", &err),
    };

    let version = update.version.clone();
    let signature = update.signature.clone();
    log::info!("[updates] background download starting version={version}");

    let bytes = match download_update(&app, &update).await {
        Ok(b) => b,
        Err(err) => return emit_updater_error(&app, "download", &err),
    };

    let sha256 = sha256_hex(&bytes);

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
        installer_file,
        signature,
        sha256,
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

    let _guard = begin_update()?;
    let started = Instant::now();
    log::info!("[updates] cached installer install requested");

    let cache_dir =
        cached_update_installer_dir(&app).ok_or("cannot determine app data directory")?;
    let meta_path = cache_dir.join("update-meta.json");

    let load_started = Instant::now();
    let meta_str =
        std::fs::read_to_string(&meta_path).map_err(|e| format!("no cached update found: {e}"))?;
    let meta: UpdateMeta =
        serde_json::from_str(&meta_str).map_err(|e| format!("invalid update meta: {e}"))?;
    log::info!(
        "[updates] cached installer metadata loaded elapsed_ms={} total_elapsed_ms={}",
        load_started.elapsed().as_millis(),
        started.elapsed().as_millis()
    );

    let exe_path = cached_installer_path(&cache_dir, &meta);
    if !exe_path.is_file() {
        return Err("installer exe not found - please download again".into());
    }

    // The cached installer lives in a user-writable directory, so "verified at
    // download time" is not enough: re-verify the on-disk bytes against the
    // configured updater public key right before launch (mirrors what
    // tauri-plugin-updater does before a foreground install). Any tampering or
    // corruption fails here and the stale cache is dropped.
    let verify_started = Instant::now();
    let bytes =
        std::fs::read(&exe_path).map_err(|e| format!("cannot read cached installer: {e}"))?;
    if let Err(err) = verify_cached_installer(&app, &meta, &bytes) {
        let _ = std::fs::remove_dir_all(&cache_dir);
        log::warn!("[updates] cached installer verification failed: {err}");
        return Err(err);
    }
    log::info!(
        "[updates] cached installer verified elapsed_ms={} total_elapsed_ms={}",
        verify_started.elapsed().as_millis(),
        started.elapsed().as_millis()
    );

    log::info!(
        "[updates] launching installer version={} exe={}",
        meta.version,
        exe_path.display(),
    );

    let stop_started = Instant::now();
    backend::stop(&app);
    log::info!(
        "[updates] backend stop requested elapsed_ms={} total_elapsed_ms={}",
        stop_started.elapsed().as_millis(),
        started.elapsed().as_millis()
    );

    emit(&app, "update:install-start", &serde_json::json!({}));

    // Launch the NSIS installer in the same updater mode Tauri uses for
    // passive Windows installs, while skipping QwenPaw's optional PATH prompt.
    let spawn_started = Instant::now();
    let child = std::process::Command::new(&exe_path)
        .arg("/P")
        .arg("/R")
        .arg("/UPDATE")
        .arg("/NO_QWENPAW_PATH")
        .spawn()
        .map_err(|e| format!("failed to launch installer: {e}"))?;
    log::info!(
        "[updates] installer process spawned pid={} elapsed_ms={} total_elapsed_ms={}",
        child.id(),
        spawn_started.elapsed().as_millis(),
        started.elapsed().as_millis()
    );

    let cleanup_started = Instant::now();
    app.cleanup_before_exit();
    log::info!(
        "[updates] app cleanup before exit complete elapsed_ms={} total_elapsed_ms={}",
        cleanup_started.elapsed().as_millis(),
        started.elapsed().as_millis()
    );
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

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut out = String::with_capacity(digest.len() * 2);
    for byte in digest {
        use std::fmt::Write;
        let _ = write!(out, "{byte:02x}");
    }
    out
}

/// Read the updater public key from the (build-injected) Tauri config so we can
/// verify cached installers with the exact key the plugin uses.
fn updater_pubkey(app: &AppHandle) -> Option<String> {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|cfg| cfg.get("pubkey"))
        .and_then(|val| val.as_str())
        .map(|s| s.to_string())
}

/// Verify `data` against a base64-encoded minisign signature and public key,
/// mirroring `tauri-plugin-updater`'s own `verify_signature`.
fn verify_minisign(data: &[u8], signature_b64: &str, pubkey_b64: &str) -> Result<(), String> {
    let pubkey_text = base64_to_string(pubkey_b64)?;
    let public_key = PublicKey::decode(pubkey_text.trim()).map_err(|e| e.to_string())?;

    let signature_text = base64_to_string(signature_b64)?;
    let signature = Signature::decode(signature_text.trim()).map_err(|e| e.to_string())?;

    public_key
        .verify(data, &signature, true)
        .map_err(|e| e.to_string())
}

fn base64_to_string(value: &str) -> Result<String, String> {
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(value.trim())
        .map_err(|e| e.to_string())?;
    String::from_utf8(decoded).map_err(|e| e.to_string())
}

/// Pre-launch integrity + authenticity gate for a previously downloaded
/// installer. Cheap SHA-256 corruption check first, then the cryptographic
/// signature check that actually closes the "user-writable cache" gap.
fn verify_cached_installer(app: &AppHandle, meta: &UpdateMeta, bytes: &[u8]) -> Result<(), String> {
    if !meta.sha256.is_empty() && sha256_hex(bytes) != meta.sha256 {
        return Err("cached installer is corrupted - please download again".into());
    }
    if meta.signature.trim().is_empty() {
        return Err("cached installer has no signature - please download again".into());
    }
    let pubkey = updater_pubkey(app).ok_or("cannot read updater public key from config")?;
    verify_minisign(bytes, &meta.signature, &pubkey)
        .map_err(|err| format!("cached installer signature invalid: {err}"))
}

async fn check_installable_update(
    app: &AppHandle,
) -> Result<Option<tauri_plugin_updater::Update>, tauri_plugin_updater::Error> {
    let updater = app
        .updater_builder()
        .on_before_exit({
            let app = app.clone();
            move || {
                backend::stop(&app);
                app.cleanup_before_exit();
            }
        })
        .build()?;

    updater.check().await
}

async fn download_update(
    app: &AppHandle,
    update: &tauri_plugin_updater::Update,
) -> Result<Vec<u8>, tauri_plugin_updater::Error> {
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
        .await?;

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
    let a = a.trim_start_matches('v');
    let b = b.trim_start_matches('v');
    match (Version::parse(a), Version::parse(b)) {
        (Ok(va), Ok(vb)) => va <= vb,
        // If either version is unparseable we cannot prove the cached update is
        // newer than the running app, so treat it as stale (true) and let the
        // caller drop the cache rather than advertising an unverifiable update.
        (Err(err), _) => {
            log::warn!(
                "[updates] cannot parse cached update version {a}, treating as stale: {err}"
            );
            true
        }
        (_, Err(err)) => {
            log::warn!(
                "[updates] cannot parse current app version {b}, treating cache as stale: {err}"
            );
            true
        }
    }
}

fn emit<S: Serialize>(app: &AppHandle, name: &str, payload: &S) {
    if let Err(err) = app.emit(name, payload) {
        log::warn!("[updates] failed to emit {name}: {err}");
    }
}

fn emit_error(app: &AppHandle, stage: &'static str, err: &dyn std::fmt::Display) {
    emit_error_kind(app, stage, "other", &err.to_string());
}

/// Emit an update error whose `kind` is derived from the concrete
/// `tauri-plugin-updater` error variant rather than fragile string matching on
/// the (library-/locale-dependent) message text.
fn emit_updater_error(app: &AppHandle, stage: &'static str, err: &tauri_plugin_updater::Error) {
    emit_error_kind(app, stage, classify_updater_error(err), &err.to_string());
}

fn emit_error_kind(app: &AppHandle, stage: &'static str, kind: &'static str, message: &str) {
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

fn classify_updater_error(err: &tauri_plugin_updater::Error) -> &'static str {
    use tauri_plugin_updater::Error as E;
    match err {
        E::Reqwest(_)
        | E::Network(_)
        | E::Http(_)
        | E::ReleaseNotFound
        | E::EmptyEndpoints
        | E::InsecureTransportProtocol
        | E::UrlParse(_) => "network",
        E::Minisign(_) | E::SignatureUtf8(_) | E::Base64(_) => "signature",
        _ => "other",
    }
}
