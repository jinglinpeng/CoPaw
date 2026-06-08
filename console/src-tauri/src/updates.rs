//! Tauri commands for desktop auto-updates via tauri-plugin-updater.

use std::io::Read as _;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_updater::UpdaterExt;
use tokio::sync::Mutex;

use crate::backend;

// ── Shared state for background-downloaded update ─────────────────────────────

pub(crate) struct UpdateCache(pub Arc<Mutex<Option<CachedUpdate>>>);

pub(crate) struct CachedUpdate {
    #[allow(dead_code)]
    update: tauri_plugin_updater::Update,
}

impl Default for UpdateCache {
    fn default() -> Self {
        Self(Arc::new(Mutex::new(None)))
    }
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UpdateMeta {
    version: String,
    ready_at: String,
    #[serde(default)]
    extracted: bool,
}

// ── Original commands (unchanged) ─────────────────────────────────────────────

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

// ── New commands: background download + deferred install ───────────────────────

#[tauri::command]
pub(crate) fn download_desktop_update(
    app: AppHandle,
    cache: tauri::State<'_, UpdateCache>,
) -> Result<(), String> {
    let cache = cache.0.clone();
    tauri::async_runtime::spawn(async move {
        run_background_download(app, cache).await;
    });
    Ok(())
}

async fn run_background_download(app: AppHandle, cache: Arc<Mutex<Option<CachedUpdate>>>) {
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
                log::info!("[updates] background download complete");
            },
        )
        .await;

    let bytes = match bytes {
        Ok(b) => b,
        Err(err) => return emit_error(&app, "download", &err),
    };

    // Final 100% progress frame.
    let _ = app.emit(
        "update:download-progress",
        serde_json::json!({
            "downloaded": downloaded,
            "total": Some(downloaded),
        }),
    );

    // Extract the NSIS installer from the zip and persist to disk.
    let updates_dir = match updates_dir(&app) {
        Some(d) => d,
        None => return emit_error(&app, "download", &"cannot determine app data directory"),
    };

    if let Err(err) = std::fs::create_dir_all(&updates_dir) {
        return emit_error(&app, "download", &err);
    }

    if let Err(err) = extract_installer(&bytes, &updates_dir, &version) {
        return emit_error(&app, "download", &format!("extract failed: {err}"));
    }

    // Pre-extract the NSIS installer with 7z so "Restart Now" is instant.
    let extracted = match pre_extract_nsis(&app, &updates_dir) {
        Ok(()) => {
            log::info!("[updates] NSIS pre-extraction complete");
            true
        }
        Err(err) => {
            log::warn!("[updates] NSIS pre-extraction failed (will fall back to NSIS): {err}");
            false
        }
    };

    emit(
        &app,
        "update:extracting",
        &serde_json::json!({ "extracted": extracted }),
    );

    // Write metadata.
    let meta = UpdateMeta {
        version: version.clone(),
        ready_at: chrono_now_iso(),
        extracted,
    };
    let meta_path = updates_dir.join("update-meta.json");
    if let Err(err) = std::fs::write(&meta_path, serde_json::to_string_pretty(&meta).unwrap()) {
        return emit_error(&app, "download", &err);
    }

    log::info!("[updates] background download ready: version={version}");

    // Cache the Update object for potential fallback via install_desktop_update.
    {
        let mut lock = cache.lock().await;
        *lock = Some(CachedUpdate { update });
    }

    emit(
        &app,
        "update:download-done",
        &serde_json::json!({ "version": version }),
    );
}

#[tauri::command]
pub(crate) fn install_downloaded_update(app: AppHandle) -> Result<(), String> {
    let updates_dir = updates_dir(&app).ok_or("cannot determine app data directory")?;
    let meta_path = updates_dir.join("update-meta.json");

    let meta_str = std::fs::read_to_string(&meta_path)
        .map_err(|e| format!("no cached update found: {e}"))?;
    let meta: UpdateMeta =
        serde_json::from_str(&meta_str).map_err(|e| format!("invalid update meta: {e}"))?;

    let staged_dir = find_staged_source(&updates_dir);

    if let Some(src_dir) = staged_dir {
        // Pre-extracted path: copy files via PowerShell script for instant restart.
        let install_dir = app
            .path()
            .resource_dir()
            .map_err(|e| format!("cannot determine install directory: {e}"))?;

        log::info!(
            "[updates] installing pre-extracted update version={} src={} dest={}",
            meta.version,
            src_dir.display(),
            install_dir.display(),
        );

        backend::stop(&app);

        let script = generate_install_script(&src_dir, &install_dir, &meta.version);
        let script_path = std::env::temp_dir().join("qwenpaw-update-install.ps1");
        std::fs::write(&script_path, &script)
            .map_err(|e| format!("failed to write install script: {e}"))?;

        std::process::Command::new("powershell.exe")
            .args([
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                &script_path.display().to_string(),
            ])
            .spawn()
            .map_err(|e| format!("failed to launch install script: {e}"))?;

        app.cleanup_before_exit();
        std::process::exit(0);
    }

    // Fallback: run NSIS installer directly (slower, ~20-30s).
    let exe_path = find_installer_exe(&updates_dir)
        .ok_or("installer exe not found — please download again")?;

    log::info!(
        "[updates] falling back to NSIS installer version={} exe={}",
        meta.version,
        exe_path.display(),
    );

    backend::stop(&app);

    std::process::Command::new(&exe_path)
        .arg("/S")
        .arg("/NO_QWENPAW_PATH")
        .spawn()
        .map_err(|e| format!("failed to launch installer: {e}"))?;

    app.cleanup_before_exit();
    std::process::exit(0);
}

#[tauri::command]
pub(crate) async fn check_cached_update(app: AppHandle) -> Result<Option<String>, String> {
    let updates_dir = match updates_dir(&app) {
        Some(d) => d,
        None => return Ok(None),
    };

    let meta_path = updates_dir.join("update-meta.json");
    if !meta_path.exists() {
        return Ok(None);
    }

    let meta_str = match std::fs::read_to_string(&meta_path) {
        Ok(s) => s,
        Err(_) => {
            let _ = std::fs::remove_dir_all(&updates_dir);
            return Ok(None);
        }
    };

    let meta: UpdateMeta = match serde_json::from_str(&meta_str) {
        Ok(m) => m,
        Err(_) => {
            let _ = std::fs::remove_dir_all(&updates_dir);
            return Ok(None);
        }
    };

    // Compare with current app version. If cached version <= current, it's stale.
    let current_version = app
        .config()
        .version
        .clone()
        .unwrap_or_default();

    if version_lte(&meta.version, &current_version) {
        log::info!(
            "[updates] cleaning stale cached update: cached={} current={}",
            meta.version,
            current_version
        );
        let _ = std::fs::remove_dir_all(&updates_dir);
        return Ok(None);
    }

    // Verify either a staged extraction or an installer exe exists.
    if find_staged_source(&updates_dir).is_none() && find_installer_exe(&updates_dir).is_none() {
        let _ = std::fs::remove_dir_all(&updates_dir);
        return Ok(None);
    }

    Ok(Some(meta.version))
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn updates_dir(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_local_data_dir().ok().map(|p| p.join("updates"))
}

fn extract_installer(bytes: &[u8], dest_dir: &PathBuf, version: &str) -> Result<PathBuf, String> {
    // Detect if bytes are a raw PE executable (starts with "MZ") or a zip archive.
    if bytes.len() >= 2 && bytes[0] == b'M' && bytes[1] == b'Z' {
        // Raw exe — save directly.
        let exe_name = format!("QwenPaw-Desktop_{version}_x64-setup.exe");
        let exe_path = dest_dir.join(&exe_name);
        std::fs::write(&exe_path, bytes).map_err(|e| e.to_string())?;
        return Ok(exe_path);
    }

    // Try as zip archive.
    let reader = std::io::Cursor::new(bytes);
    let mut archive = zip::ZipArchive::new(reader).map_err(|e| e.to_string())?;

    let mut exe_path: Option<PathBuf> = None;

    for i in 0..archive.len() {
        let mut file = archive.by_index(i).map_err(|e| e.to_string())?;
        let name = file.name().to_string();

        if name.ends_with('/') {
            continue;
        }

        let out_path = dest_dir.join(&name);
        if let Some(parent) = out_path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }

        let mut out_file = std::fs::File::create(&out_path).map_err(|e| e.to_string())?;
        let mut buf = Vec::new();
        file.read_to_end(&mut buf).map_err(|e| e.to_string())?;
        std::io::Write::write_all(&mut out_file, &buf).map_err(|e| e.to_string())?;

        if name.to_lowercase().ends_with(".exe") {
            exe_path = Some(out_path);
        }
    }

    exe_path.ok_or_else(|| "no .exe found in update zip".to_string())
}

fn pre_extract_nsis(app: &AppHandle, updates_dir: &PathBuf) -> Result<(), String> {
    let exe_path =
        find_installer_exe(updates_dir).ok_or("no installer exe found for extraction")?;

    let seven_zip = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("tools")
        .join("7z.exe");

    if !seven_zip.is_file() {
        return Err(format!("7z.exe not found at {}", seven_zip.display()));
    }

    let staged_dir = updates_dir.join("staged");
    if staged_dir.exists() {
        let _ = std::fs::remove_dir_all(&staged_dir);
    }

    log::info!(
        "[updates] extracting NSIS installer with 7z: {} -> {}",
        exe_path.display(),
        staged_dir.display(),
    );

    let status = std::process::Command::new(&seven_zip)
        .arg("x")
        .arg(exe_path.display().to_string())
        .arg(format!("-o{}", staged_dir.display()))
        .arg("-y")
        .status()
        .map_err(|e| format!("failed to run 7z: {e}"))?;

    if !status.success() {
        return Err(format!("7z exited with status {status}"));
    }

    // Verify we got usable output.
    if find_staged_source(updates_dir).is_none() {
        return Err("7z extraction produced no usable app files".to_string());
    }

    Ok(())
}

fn find_staged_source(updates_dir: &PathBuf) -> Option<PathBuf> {
    let staged = updates_dir.join("staged");
    if !staged.is_dir() {
        return None;
    }
    // 7z extracts NSIS into $INSTDIR/ subdirectory.
    let instdir = staged.join("$INSTDIR");
    if instdir.is_dir() {
        return Some(instdir);
    }
    // Fallback: maybe files are directly in staged/.
    let has_exe = std::fs::read_dir(&staged).ok()?.any(|e| {
        e.ok()
            .and_then(|e| e.path().extension().map(|ext| ext == "exe"))
            .unwrap_or(false)
    });
    if has_exe {
        Some(staged)
    } else {
        None
    }
}

fn generate_install_script(src_dir: &PathBuf, install_dir: &PathBuf, version: &str) -> String {
    format!(
        r#"param()
$ErrorActionPreference = "Stop"
$installDir = @'
{install_dir}
'@.Trim()
$srcDir = @'
{src_dir}
'@.Trim()
$version = "{version}"
$logFile = Join-Path $env:TEMP "qwenpaw-update-install.log"

function Log {{ param([string]$msg) "$(Get-Date -f 'HH:mm:ss') $msg" | Out-File -Append $logFile }}

Log "Waiting for qwenpaw-desktop.exe to exit..."
$timeout = 30; $elapsed = 0
while ($elapsed -lt $timeout) {{
    if (-not (Get-Process -Name "qwenpaw-desktop" -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 500; $elapsed += 0.5
}}
$proc = Get-Process -Name "qwenpaw-desktop" -ErrorAction SilentlyContinue
if ($proc) {{
    Stop-Process -Name "qwenpaw-desktop" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}}

Log "Copying from $srcDir to $installDir"
try {{
    Copy-Item -Path (Join-Path $srcDir "*") -Destination $installDir -Recurse -Force
    Log "Copy complete"
}} catch {{
    Log "Copy failed: $_"
    exit 1
}}

# Update registry DisplayVersion
$regKey = Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue |
    Where-Object {{ $_.GetValue("DisplayName") -like "QwenPaw Desktop*" }} |
    Select-Object -First 1
if ($regKey) {{
    Set-ItemProperty -Path $regKey.PSPath -Name "DisplayVersion" -Value $version -ErrorAction SilentlyContinue
    Log "Registry updated: $($regKey.PSPath) DisplayVersion=$version"
}}

# Clean up updates directory
$updatesDir = Join-Path $installDir "updates"
Remove-Item -Path $updatesDir -Recurse -Force -ErrorAction SilentlyContinue
Log "Cleanup done"

# Launch new version
$exe = Join-Path $installDir "qwenpaw-desktop.exe"
Log "Launching $exe"
Start-Process -FilePath $exe
"#,
        install_dir = install_dir.display(),
        src_dir = src_dir.display(),
        version = version,
    )
}

fn find_installer_exe(updates_dir: &PathBuf) -> Option<PathBuf> {
    std::fs::read_dir(updates_dir).ok()?.find_map(|entry| {
        let path = entry.ok()?.path();
        if path.extension().and_then(|e| e.to_str()) == Some("exe") {
            Some(path)
        } else {
            None
        }
    })
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

fn chrono_now_iso() -> String {
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
