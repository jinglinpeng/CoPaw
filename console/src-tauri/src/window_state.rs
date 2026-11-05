//! Window geometry shared by normal restarts and in-memory WebView recovery.
//!
//! Only an accepted application exit writes disk. Tray hiding and undecided
//! close prompts never normalize/hide the document or persist visibility.

mod store;

use std::{
    path::PathBuf,
    sync::{Mutex, MutexGuard},
};
use store::{Display, SavedState};
use tauri::{
    utils::config::WindowConfig, AppHandle, Manager, PhysicalPosition, PhysicalSize, WebviewWindow,
};

#[derive(Default)]
pub(crate) struct Controller {
    lifecycle: Mutex<()>,
    cached: Mutex<Option<SavedState>>,
}

impl Controller {
    pub(crate) fn lock(&self) -> MutexGuard<'_, ()> {
        self.lifecycle
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn directory(app: &AppHandle) -> Result<PathBuf, String> {
    crate::updates::coordination::installation(app).map(PathBuf::from)
}

fn display(monitor: &tauri::Monitor) -> Display {
    Display {
        name: monitor.name().cloned(),
        origin: [monitor.position().x, monitor.position().y],
        work_origin: [
            monitor.work_area().position.x,
            monitor.work_area().position.y,
        ],
        work_size: [
            monitor.work_area().size.width,
            monitor.work_area().size.height,
        ],
        scale: monitor.scale_factor(),
    }
}

pub(crate) fn remember(window: &WebviewWindow, snapshot: WindowState) {
    let saved = (|| {
        let position = snapshot.position?;
        let size = snapshot.size?;
        let monitor = window.current_monitor().ok()??;
        SavedState::capture(
            [position.x, position.y],
            [size.width, size.height],
            snapshot.maximized,
            &display(&monitor),
        )
    })();
    if let Some(saved) = saved {
        *window
            .app_handle()
            .state::<Controller>()
            .cached
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(saved);
    }
}

/// Called by the exit worker while holding the lifecycle lock, before destroy.
/// If recovery has already destroyed its failed document, use its memory-only
/// snapshot. A failed write must never block closing or corrupt previous state.
pub(crate) fn save_on_exit(app: &AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        let config = app
            .config()
            .app
            .windows
            .iter()
            .find(|config| config.label == "main")
            .ok_or("Missing main window config")?;
        remember(&window, WindowState::capture(&window, config));
    }
    let saved = app
        .state::<Controller>()
        .cached
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    if let Some(saved) = saved {
        store::write(&directory(app)?, &saved)?;
    }
    Ok(())
}

/// Cold startup restores normal bounds first and maximizes last. Transient
/// visibility, focus, minimization and fullscreen are intentionally never loaded.
pub(crate) fn restore_on_start(app: &AppHandle) -> Result<(), String> {
    let Some(saved) = store::read(&directory(app)?)? else {
        return Ok(());
    };
    let Some(window) = app.get_webview_window("main") else {
        return Ok(());
    };
    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|config| config.label == "main")
        .ok_or("Missing main window config")?;
    let monitors = window
        .available_monitors()
        .map_err(|error| error.to_string())?;
    let displays: Vec<_> = monitors.iter().map(display).collect();
    let primary = window
        .primary_monitor()
        .ok()
        .flatten()
        .and_then(|monitor| {
            displays
                .iter()
                .position(|display| display.origin == [monitor.position().x, monitor.position().y])
        })
        .unwrap_or(0);
    let inner = window.inner_size().map_err(|error| error.to_string())?;
    let outer = window.outer_size().map_err(|error| error.to_string())?;
    let scale = window.scale_factor().map_err(|error| error.to_string())?;
    let frame = [
        f64::from(outer.width.saturating_sub(inner.width)) / scale,
        f64::from(outer.height.saturating_sub(inner.height)) / scale,
    ];
    let placement = saved
        .placement(
            &displays,
            primary,
            [
                config.min_width.unwrap_or(1.0),
                config.min_height.unwrap_or(1.0),
            ],
            frame,
        )
        .ok_or("No usable display for saved window state")?;
    window
        .set_min_size(Some(tauri::LogicalSize::new(
            placement.min_size[0],
            placement.min_size[1],
        )))
        .map_err(|error| error.to_string())?;
    window
        .set_position(PhysicalPosition::new(
            placement.position[0],
            placement.position[1],
        ))
        .map_err(|error| error.to_string())?;
    window
        .set_size(PhysicalSize::new(placement.size[0], placement.size[1]))
        .map_err(|error| error.to_string())?;
    if placement.maximized {
        window.maximize().map_err(|error| error.to_string())?;
    }
    *app.state::<Controller>()
        .cached
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(saved);
    Ok(())
}

#[derive(Clone, Copy)]
pub(crate) struct WindowState {
    visible: bool,
    focused: bool,
    minimized: bool,
    maximized: bool,
    fullscreen: bool,
    position: Option<PhysicalPosition<i32>>,
    size: Option<PhysicalSize<u32>>,
}

impl WindowState {
    pub(crate) fn capture(window: &WebviewWindow, config: &WindowConfig) -> Self {
        let visible = window.is_visible().unwrap_or(config.visible);
        let focused = window.is_focused().unwrap_or(config.focus);
        let minimized = window.is_minimized().unwrap_or(false);
        let maximized = window.is_maximized().unwrap_or(config.maximized);
        let fullscreen = window.is_fullscreen().unwrap_or(config.fullscreen);

        // Normalize while hidden so the normal (non-maximized) bounds can be read.
        let _ = window.hide();
        // AppKit fullscreen transitions are asynchronous. There is no public
        // Tauri normal-bounds getter for that transition; preserve the previous
        // ordinary snapshot rather than saving the fullscreen/animation frame.
        let mut normalized = !(cfg!(target_os = "macos") && fullscreen);
        if normalized && fullscreen {
            normalized &= window.set_fullscreen(false).is_ok();
        }
        if normalized && minimized {
            normalized &= window.unminimize().is_ok();
        }
        if normalized && maximized {
            normalized &= window.unmaximize().is_ok();
        }
        normalized &= matches!(window.is_fullscreen(), Ok(false))
            && matches!(window.is_minimized(), Ok(false))
            && matches!(window.is_maximized(), Ok(false));

        // Do not overwrite a previous normal snapshot with fullscreen/maximized
        // bounds if the native transition failed or has not completed.
        let position = normalized.then(|| window.outer_position().ok()).flatten();
        let size = normalized.then(|| window.inner_size().ok()).flatten();

        Self {
            visible,
            focused,
            minimized,
            maximized,
            fullscreen,
            position,
            size,
        }
    }

    #[cfg(windows)]
    pub(crate) fn from_config(config: &WindowConfig) -> Self {
        Self {
            visible: config.visible,
            focused: config.focus,
            minimized: false,
            maximized: config.maximized,
            fullscreen: config.fullscreen,
            position: None,
            size: None,
        }
    }
}

#[cfg(windows)]
pub(crate) fn restore_state(window: &WebviewWindow, state: WindowState) -> Result<(), String> {
    if let Some(position) = state.position {
        window
            .set_position(position)
            .map_err(|err| err.to_string())?;
    }
    if let Some(size) = state.size {
        window.set_size(size).map_err(|err| err.to_string())?;
    }
    if state.fullscreen {
        window.set_fullscreen(true).map_err(|err| err.to_string())?;
    } else if state.maximized {
        window.maximize().map_err(|err| err.to_string())?;
    }
    if state.visible {
        window.show().map_err(|err| err.to_string())?;
        if state.minimized {
            window.minimize().map_err(|err| err.to_string())?;
        } else if state.focused {
            window.set_focus().map_err(|err| err.to_string())?;
        }
    } else {
        if state.minimized {
            window.minimize().map_err(|err| err.to_string())?;
        }
        window.hide().map_err(|err| err.to_string())?;
    }
    Ok(())
}
