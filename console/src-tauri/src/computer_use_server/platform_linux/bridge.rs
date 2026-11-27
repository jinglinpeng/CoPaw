//! The platform process: one Python child that owns every desktop call.
//!
//! Requests and replies are single JSON lines over the child's standard
//! streams. Every platform operation of the shared layers funnels through
//! `request`, which serializes calls, bounds each one with a timeout, and
//! restarts the child after a timeout or a crash so one wedged application
//! cannot wedge the helper.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const SIDECAR_SOURCE: &str = include_str!("sidecar.py");
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(10);
const OBSERVE_TIMEOUT: Duration = Duration::from_secs(20);

struct Sidecar {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<String>,
    next_id: u64,
}

static SIDECAR: Mutex<Option<Sidecar>> = Mutex::new(None);

/// Send one operation to the platform process and wait for its reply.
pub(super) fn request(op: &str, args: Value) -> Result<Value, (&'static str, String)> {
    let timeout = if matches!(op, "observe" | "tree") {
        OBSERVE_TIMEOUT
    } else {
        DEFAULT_TIMEOUT
    };
    let mut guard = SIDECAR.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    if guard.is_none() {
        *guard = Some(start()?);
    }
    let sidecar = guard.as_mut().expect("platform process was just started");
    sidecar.next_id += 1;
    let id = sidecar.next_id;
    let line = serde_json::to_string(&json!({"id": id, "op": op, "args": args}))
        .map_err(|error| ("action_failed", error.to_string()))?;
    if writeln!(sidecar.stdin, "{line}")
        .and_then(|_| sidecar.stdin.flush())
        .is_err()
    {
        discard(&mut guard);
        return Err((
            "action_failed",
            "The Linux platform process exited; retry the action.".to_string(),
        ));
    }
    let deadline = Instant::now() + timeout;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match sidecar.lines.recv_timeout(remaining) {
            Ok(text) => {
                let Ok(reply) = serde_json::from_str::<Value>(&text) else {
                    continue;
                };
                if reply.get("id").and_then(Value::as_u64) != Some(id) {
                    continue;
                }
                return parse_reply(reply);
            }
            Err(RecvTimeoutError::Timeout) => {
                discard(&mut guard);
                return Err((
                    "action_failed",
                    format!("The Linux platform process did not answer {op} in time."),
                ));
            }
            Err(RecvTimeoutError::Disconnected) => {
                discard(&mut guard);
                return Err((
                    "action_failed",
                    "The Linux platform process exited; retry the action.".to_string(),
                ));
            }
        }
    }
}

fn parse_reply(reply: Value) -> Result<Value, (&'static str, String)> {
    if reply.get("ok").and_then(Value::as_bool) == Some(true) {
        return Ok(reply.get("result").cloned().unwrap_or(Value::Null));
    }
    let error = reply.get("error").cloned().unwrap_or_default();
    let code = error.get("code").and_then(Value::as_str).unwrap_or("");
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("The Linux platform action failed.")
        .to_string();
    Err((static_code(code), message))
}

/// The shared layers speak a fixed vocabulary of error codes.
fn static_code(code: &str) -> &'static str {
    match code {
        "accessibility_permission_required" => "accessibility_permission_required",
        "app_not_found" => "app_not_found",
        "capture_failed" => "capture_failed",
        "element_not_found" => "element_not_found",
        "element_unavailable" => "element_unavailable",
        "focus_failed" => "focus_failed",
        "focus_not_editable" => "focus_not_editable",
        "input_failed" => "input_failed",
        "invalid_request" => "invalid_request",
        "point_outside_viewport" => "point_outside_viewport",
        "postcondition_failed" => "postcondition_failed",
        "stale_observation" => "stale_observation",
        "stale_window" => "stale_window",
        "target_not_at_point" => "target_not_at_point",
        "unsupported_operation" => "unsupported_operation",
        "visual_unavailable" => "visual_unavailable",
        "window_not_found" => "window_not_found",
        _ => "action_failed",
    }
}

fn discard(guard: &mut Option<Sidecar>) {
    if let Some(mut sidecar) = guard.take() {
        let _ = sidecar.child.kill();
        let _ = sidecar.child.wait();
    }
}

fn start() -> Result<Sidecar, (&'static str, String)> {
    let python = std::env::var_os("QWENPAW_CU_SIDECAR_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/usr/bin/python3"));
    let script = sidecar_script().map_err(|error| ("action_failed", error))?;
    let mut child = Command::new(&python)
        .arg("-u")
        .arg(&script)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|error| {
            (
                "accessibility_permission_required",
                format!(
                    "Could not start the Linux platform process with {}: {error}",
                    python.display()
                ),
            )
        })?;
    let stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let (sender, lines) = mpsc::channel();
    std::thread::Builder::new()
        .name("computer-use-sidecar".to_string())
        .spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if sender.send(line).is_err() {
                    break;
                }
            }
        })
        .map_err(|error| ("action_failed", error.to_string()))?;
    Ok(Sidecar {
        child,
        stdin,
        lines,
        next_id: 0,
    })
}

/// Use an explicit script during development; otherwise write the copy that
/// is compiled into this binary, so the helper stays a single file.
fn sidecar_script() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("QWENPAW_CU_SIDECAR") {
        return Ok(PathBuf::from(path));
    }
    use std::os::unix::fs::PermissionsExt;
    let directory = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .filter(|path| path.is_dir())
        .unwrap_or_else(std::env::temp_dir);
    let path = directory.join(format!(
        "qwenpaw-computer-use-sidecar-{}.py",
        std::process::id()
    ));
    std::fs::write(&path, SIDECAR_SOURCE)
        .map_err(|error| format!("failed to write the platform script: {error}"))?;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("failed to secure the platform script: {error}"))?;
    Ok(path)
}
