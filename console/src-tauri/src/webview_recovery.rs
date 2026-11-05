use std::{
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

use tauri::{utils::config::WindowConfig, AppHandle, Manager, WebviewWindow, WebviewWindowBuilder};
use webview2_com::{
    BrowserProcessExitedEventHandler,
    Microsoft::Web::WebView2::Win32::{
        ICoreWebView2Environment5, COREWEBVIEW2_BROWSER_PROCESS_EXIT_KIND_FAILED,
    },
};
use windows::core::Interface;

use crate::window_state::{self, WindowState};

const WEBVIEW_TIMEOUT: Duration = Duration::from_secs(5);
const DESTROY_TIMEOUT: Duration = Duration::from_secs(2);
const RETRY_DELAY: Duration = Duration::from_millis(250);
const MAX_ATTEMPTS: usize = 3;

static ACTIVE_RECOVERIES: AtomicUsize = AtomicUsize::new(0);

struct RecoveryGuard;

impl RecoveryGuard {
    fn new() -> Self {
        ACTIVE_RECOVERIES.fetch_add(1, Ordering::AcqRel);
        Self
    }
}

impl Drop for RecoveryGuard {
    fn drop(&mut self) {
        ACTIVE_RECOVERIES.fetch_sub(1, Ordering::AcqRel);
    }
}

pub fn install(window: &WebviewWindow) -> Result<(), String> {
    let app = window.app_handle().clone();
    let label = window.label().to_owned();
    let (sender, receiver) = mpsc::sync_channel(1);

    window
        .with_webview(move |webview| {
            let result = (|| {
                let environment = webview
                    .environment()
                    .cast::<ICoreWebView2Environment5>()
                    .map_err(|err| err.to_string())?;
                let handler =
                    BrowserProcessExitedEventHandler::create(Box::new(move |_sender, args| {
                        let Some(args) = args else {
                            return Ok(());
                        };
                        let mut exit_kind = Default::default();
                        let mut process_id = 0;
                        unsafe {
                            args.BrowserProcessExitKind(&mut exit_kind)?;
                            args.BrowserProcessId(&mut process_id)?;
                        }
                        if exit_kind == COREWEBVIEW2_BROWSER_PROCESS_EXIT_KIND_FAILED {
                            schedule_recovery(app.clone(), label.clone(), process_id);
                        }
                        Ok(())
                    }));

                let mut token = 0;
                unsafe { environment.add_BrowserProcessExited(&handler, &mut token) }
                    .map_err(|err| err.to_string())?;

                // Prove the handler is attached to a live browser before recovery proceeds.
                let core = unsafe { webview.controller().CoreWebView2() }
                    .map_err(|err| err.to_string())?;
                let mut process_id = 0;
                unsafe { core.BrowserProcessId(&mut process_id) }.map_err(|err| err.to_string())?;
                (process_id != 0)
                    .then_some(())
                    .ok_or_else(|| "WebView2 browser process is not running".to_owned())
            })();
            let _ = sender.send(result);
        })
        .map_err(|err| err.to_string())?;

    receiver
        .recv_timeout(WEBVIEW_TIMEOUT)
        .map_err(|err| format!("timed out installing browser-process recovery: {err}"))?
}

pub fn is_active() -> bool {
    ACTIVE_RECOVERIES.load(Ordering::Acquire) != 0
}

fn schedule_recovery(app: AppHandle, label: String, process_id: u32) {
    let dispatcher = app.clone();
    // Return from the COM callback before creating or destroying WebView2 controllers.
    if let Err(err) = dispatcher.run_on_main_thread(move || {
        if let Err(err) = thread::Builder::new()
            .name("qwenpaw-webview-recovery".into())
            .spawn(move || {
                log::warn!(
                    "[webview] browser process {process_id} exited unexpectedly; rebuilding {label}"
                );
                if let Err(err) = recreate(&app, &label) {
                    log::error!("[webview] failed to rebuild {label}: {err}");
                }
            })
        {
            log::error!("[webview] failed to start recovery worker: {err}");
        }
    }) {
        log::error!("[webview] failed to schedule browser-process recovery: {err}");
    }
}

fn recreate(app: &AppHandle, label: &str) -> Result<(), String> {
    let _active = RecoveryGuard::new();
    // Exit and crash recovery share ownership of the document snapshot. The
    // exit worker waits here without blocking the UI dispatcher.
    let controller = app.state::<window_state::Controller>();
    let _guard = controller.lock();
    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|config| config.label == label)
        .cloned()
        .ok_or_else(|| format!("window configuration not found: {label}"))?;
    let state = app
        .get_webview_window(label)
        .map(|window| {
            let state = WindowState::capture(&window, &config);
            window_state::remember(&window, state);
            state
        })
        .unwrap_or_else(|| WindowState::from_config(&config));

    let mut last_error = String::new();
    for attempt in 1..=MAX_ATTEMPTS {
        if crate::tray::is_quitting(app) {
            return Ok(());
        }
        match recreate_window(app, label, &config, state) {
            Ok(()) => {
                log::info!("[webview] rebuilt {label} after browser-process failure");
                return Ok(());
            }
            Err(err) => {
                last_error = err;
                log::warn!(
                    "[webview] recovery attempt {attempt}/{MAX_ATTEMPTS} for {label} failed: {last_error}"
                );
                if attempt < MAX_ATTEMPTS {
                    thread::sleep(RETRY_DELAY);
                }
            }
        }
    }

    log::error!("[webview] recovery exhausted; keeping the application and backend alive");
    Err(last_error)
}

fn recreate_window(
    app: &AppHandle,
    label: &str,
    config: &WindowConfig,
    state: WindowState,
) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(label) {
        window.destroy().map_err(|err| err.to_string())?;
        wait_until_destroyed(app, label)?;
    }

    let window = WebviewWindowBuilder::from_config(app, config)
        .map_err(|err| err.to_string())?
        .visible(false)
        .focused(false)
        .maximized(false)
        .fullscreen(false)
        .build()
        .map_err(|err| err.to_string())?;
    if crate::tray::is_quitting(app) {
        return window.destroy().map_err(|err| err.to_string());
    }
    install(&window)?;
    window_state::restore_state(&window, state)?;
    Ok(())
}

fn wait_until_destroyed(app: &AppHandle, label: &str) -> Result<(), String> {
    let deadline = Instant::now() + DESTROY_TIMEOUT;
    while app.get_webview_window(label).is_some() {
        if Instant::now() >= deadline {
            return Err(format!("timed out destroying window: {label}"));
        }
        thread::sleep(Duration::from_millis(10));
    }
    Ok(())
}
