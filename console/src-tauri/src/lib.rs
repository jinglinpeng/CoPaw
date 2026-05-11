use std::net::TcpListener;
use tauri::Manager;
use tauri_plugin_shell::process::CommandEvent;
use tauri_plugin_shell::ShellExt;

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

            // Spawn backend sidecar (desktop_entry.py handles init + app)
            let command = app
                .shell()
                .sidecar("qwenpaw-backend")
                .expect("failed to find sidecar binary")
                .env("QWENPAW_DESKTOP_PORT", backend_port.to_string());
            drop(port_guard);
            let (mut rx, _child) = command
                .spawn()
                .expect("failed to spawn sidecar");

            // Log backend output
            let _handle = app.handle().clone();
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
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
