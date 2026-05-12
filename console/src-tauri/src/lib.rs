use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command as StdCommand, Stdio};
use std::sync::Mutex;
use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

#[derive(Default)]
struct BackendProcess(Mutex<Option<CommandChild>>);

impl BackendProcess {
    fn set(&self, child: CommandChild) {
        *self.0.lock().expect("backend process lock poisoned") = Some(child);
    }

    fn clear(&self) {
        self.0
            .lock()
            .expect("backend process lock poisoned")
            .take();
    }

    fn kill(&self) {
        let child = self
            .0
            .lock()
            .expect("backend process lock poisoned")
            .take();
        if let Some(child) = child {
            if let Err(err) = child.kill() {
                log::warn!("[backend] failed to stop process: {err}");
            }
        }
    }
}

#[tauri::command]
fn backend_port(port: tauri::State<'_, u16>) -> u16 {
    *port
}

fn pick_backend_port() -> std::io::Result<(u16, TcpListener)> {
    for port in 8088..8188 {
        if let Ok(listener) = TcpListener::bind(("127.0.0.1", port)) {
            return Ok((port, listener));
        }
    }

    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok((listener.local_addr()?.port(), listener))
}

fn command_exists(command: &str) -> bool {
    StdCommand::new(command)
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn local_python(repo_root: &Path) -> Option<String> {
    let candidates = if cfg!(windows) {
        vec![
            repo_root.join(".venv/Scripts/python.exe"),
            repo_root.join("venv/Scripts/python.exe"),
        ]
    } else {
        vec![
            repo_root.join(".venv/bin/python"),
            repo_root.join("venv/bin/python"),
        ]
    };

    candidates
        .into_iter()
        .find(|path| path.is_file())
        .map(|path| path.display().to_string())
}

fn python_command(repo_root: &Path) -> String {
    local_python(repo_root).unwrap_or_else(|| {
        if command_exists("python3") {
            "python3".to_string()
        } else {
            "python".to_string()
        }
    })
}

fn setup_backend(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    if cfg!(debug_assertions) {
        app.handle().plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )?;
    }

    let (backend_port, port_guard) =
        pick_backend_port().map_err(|e| format!("failed to reserve backend port: {e}"))?;
    app.manage(backend_port);

    let command = (if cfg!(debug_assertions) {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let source_path = repo_root.join("src");
        if command_exists("uv") {
            app.shell()
                .command("uv")
                .args(["run", "python", "-m", "qwenpaw.desktop_entry"])
                .current_dir(repo_root)
                .env("PYTHONPATH", source_path.display().to_string())
        } else {
            let python = python_command(&repo_root);
            app.shell()
                .command(python)
                .args(["-m", "qwenpaw.desktop_entry"])
                .current_dir(repo_root)
                .env("PYTHONPATH", source_path.display().to_string())
        }
    } else {
        app.shell()
            .sidecar("qwenpaw-backend")
            .map_err(|e| format!("failed to find sidecar binary: {e}"))?
    })
    .env("QWENPAW_DESKTOP_PORT", backend_port.to_string());

    let (mut rx, child) = command
        .spawn()
        .map_err(|e| format!("failed to spawn backend: {e}"))?;

    // Release the reserved port only after the backend process has been spawned,
    // so the OS does not reassign the port between bind and listen.
    drop(port_guard);

    app.state::<BackendProcess>().set(child);

    // Log backend output
    let app_handle = app.handle().clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    log::info!("[backend] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Stderr(line) => {
                    log::error!("[backend] {}", String::from_utf8_lossy(&line));
                }
                _ => {}
            }
        }
        log::warn!("[backend] process exited");
        app_handle.state::<BackendProcess>().clear();
    });

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let build_result = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![backend_port])
        .manage(BackendProcess::default())
        .setup(|app| setup_backend(app))
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::CloseRequested { .. }) {
                window.state::<BackendProcess>().kill();
            }
        })
        .build(tauri::generate_context!());

    match build_result {
        Ok(app) => {
            app.run(|app_handle, event| {
                if let RunEvent::ExitRequested { .. } = event {
                    app_handle.state::<BackendProcess>().kill();
                }
            });
        }
        Err(e) => {
            eprintln!("[QwenPaw Desktop] Fatal startup error: {e}");
            std::process::exit(1);
        }
    }
}
