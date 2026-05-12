use std::net::TcpListener;
use std::path::PathBuf;
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![backend_port])
        .manage(BackendProcess::default())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let (backend_port, port_guard) =
                pick_backend_port().expect("failed to reserve backend port");
            app.manage(backend_port);

            let command = (if cfg!(debug_assertions) {
                let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
                let source_path = repo_root.join("src");
                app.shell()
                    .command("uv")
                    .args(["run", "python", "-m", "qwenpaw.desktop_entry"])
                    .current_dir(repo_root)
                    .env("PYTHONPATH", source_path.display().to_string())
            } else {
                app.shell()
                    .sidecar("qwenpaw-backend")
                    .expect("failed to find sidecar binary")
            })
            .env("QWENPAW_DESKTOP_PORT", backend_port.to_string());
            drop(port_guard);
            let (mut rx, child) = command
                .spawn()
                .expect("failed to spawn backend");
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
        })
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::CloseRequested { .. }) {
                window.state::<BackendProcess>().kill();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::ExitRequested { .. } = event {
                app_handle.state::<BackendProcess>().kill();
            }
        });
}
