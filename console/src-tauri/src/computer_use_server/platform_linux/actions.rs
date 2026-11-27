//! Input and semantic actions, each forwarded to the platform process.
//!
//! Parameter validation and screenshot-to-screen mapping happen here with the
//! shared helpers, so a coordinate is checked against the delivered image in
//! exactly the way the Windows helper checks it.

use serde_json::{json, Map, Value};

use super::super::state::{map_point, screenshot_target, Observation, PendingAction};
use super::super::InputStep;
use super::bridge::request;

fn element_ref(observation: &Observation, element_id: &str) -> Result<Value, (&'static str, String)> {
    let element = observation.elements.get(element_id).ok_or((
        "element_not_found",
        "Element is not available in this observation.".to_string(),
    ))?;
    Ok(json!({"key": element.key, "element": element.index}))
}

/// The observation's own key, carried by every element it published.
fn observation_key(observation: &Observation) -> String {
    observation
        .elements
        .values()
        .next()
        .map(|element| element.key.clone())
        .unwrap_or_default()
}

fn integer_param(params: &Map<String, Value>, name: &str) -> Result<i64, (&'static str, String)> {
    params
        .get(name)
        .and_then(Value::as_i64)
        .filter(|value| i32::try_from(*value).is_ok())
        .ok_or(("invalid_request", format!("{name} is required.")))
}

/// Map a screenshot coordinate onto the screen and name the surface it hits.
fn screen_point(
    observation: &Observation,
    params: &Map<String, Value>,
    prefix: &str,
) -> Result<Value, (&'static str, String)> {
    let screenshot = screenshot_target(observation, params)?;
    let x = integer_param(params, &format!("{prefix}x"))?;
    let y = integer_param(params, &format!("{prefix}y"))?;
    let (x_offset, y_offset) = map_point(screenshot, x, y)?;
    Ok(json!({
        "x": screenshot.bounds[0] + x_offset as i32,
        "y": screenshot.bounds[1] + y_offset as i32,
        "surface": {"xid": screenshot.hwnd, "bounds": screenshot.bounds},
    }))
}

fn base_args(observation: &Observation) -> Map<String, Value> {
    let mut args = Map::new();
    args.insert("window".to_string(), json!(observation.window.hwnd));
    args.insert("window_bounds".to_string(), json!(observation.window_bounds));
    args
}

fn merge(args: &mut Map<String, Value>, extra: Value) {
    if let Value::Object(extra) = extra {
        args.extend(extra);
    }
}

pub(crate) fn click(
    observation: &Observation,
    params: &Map<String, Value>,
) -> Result<Value, (&'static str, String)> {
    let button = params
        .get("button")
        .and_then(Value::as_str)
        .unwrap_or("left");
    if !matches!(button, "left" | "right" | "middle") {
        return Err(("invalid_request", "Unsupported mouse button.".to_string()));
    }
    let count = params
        .get("count")
        .and_then(Value::as_u64)
        .unwrap_or(1)
        .clamp(1, 3);
    let mut args = base_args(observation);
    args.insert("button".to_string(), json!(button));
    args.insert("count".to_string(), json!(count));
    if let Some(element_id) = params.get("element_id").and_then(Value::as_str) {
        merge(&mut args, element_ref(observation, element_id)?);
    } else {
        merge(&mut args, screen_point(observation, params, "")?);
    }
    request("click", Value::Object(args))
}

pub(crate) fn scroll(
    observation: &Observation,
    params: &Map<String, Value>,
) -> Result<Value, (&'static str, String)> {
    let delta = params
        .get("delta_y")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .clamp(-1200, 1200);
    let mut args = base_args(observation);
    merge(&mut args, screen_point(observation, params, "")?);
    args.insert("delta_y".to_string(), json!(delta));
    request("scroll", Value::Object(args))
}

pub(crate) fn drag(
    observation: &Observation,
    params: &Map<String, Value>,
) -> Result<Value, (&'static str, String)> {
    let source = params.get("source_element_id").and_then(Value::as_str);
    let target = params.get("target_element_id").and_then(Value::as_str);
    let mut args = base_args(observation);
    match (source, target) {
        (Some(source), Some(target)) => {
            args.insert("source".to_string(), element_ref(observation, source)?);
            args.insert("target".to_string(), element_ref(observation, target)?);
        }
        (None, None) => {
            args.insert("start".to_string(), screen_point(observation, params, "start_")?);
            args.insert("end".to_string(), screen_point(observation, params, "end_")?);
        }
        _ => {
            return Err((
                "invalid_request",
                "Both source_element_id and target_element_id are required for an element drag."
                    .to_string(),
            ))
        }
    }
    request("drag", Value::Object(args))
}

pub(crate) fn type_text(
    observation: &Observation,
    params: &Map<String, Value>,
) -> Result<Value, (&'static str, String)> {
    let text = params
        .get("text")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or(("invalid_request", "text is required.".to_string()))?;
    let mut args = base_args(observation);
    args.insert("key".to_string(), json!(observation_key(observation)));
    args.insert("text".to_string(), json!(text));
    request("type", Value::Object(args))
}

pub(crate) fn press_key(
    observation: &Observation,
    params: &Map<String, Value>,
) -> Result<Value, (&'static str, String)> {
    let key = params
        .get("key")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or(("invalid_request", "key is required.".to_string()))?;
    let mut args = base_args(observation);
    args.insert("keys".to_string(), json!(key));
    request("key", Value::Object(args))
}

pub(crate) fn input_sequence(
    observation: &Observation,
    steps: &[InputStep],
) -> Result<Value, (&'static str, String)> {
    let steps: Vec<Value> = steps
        .iter()
        .map(|step| match step {
            InputStep::Type(text) => json!({"action": "type", "text": text}),
            InputStep::PressKey(key) => json!({"action": "press_key", "key": key}),
        })
        .collect();
    let mut args = base_args(observation);
    args.insert("steps".to_string(), json!(steps));
    request("sequence", Value::Object(args))
}

pub(crate) fn invoke_element(
    observation: &Observation,
    params: &Map<String, Value>,
    pending: Option<&PendingAction>,
) -> Result<Value, (&'static str, String)> {
    if pending.is_some() {
        return Err((
            "pending_action_unavailable",
            "This platform cannot complete the pending native edit.".to_string(),
        ));
    }
    let element_id = params
        .get("element_id")
        .and_then(Value::as_str)
        .ok_or(("invalid_request", "element_id is required.".to_string()))?;
    let mut args = base_args(observation);
    merge(&mut args, element_ref(observation, element_id)?);
    if let Some(action) = params.get("action").and_then(Value::as_str) {
        args.insert("action".to_string(), json!(action));
    }
    request("invoke", Value::Object(args))
}

pub(crate) fn set_value(
    observation: &Observation,
    params: &Map<String, Value>,
) -> Result<(Value, Option<PendingAction>), (&'static str, String)> {
    let value = params
        .get("value")
        .and_then(Value::as_str)
        .ok_or(("invalid_request", "value is required.".to_string()))?;
    let element_id = params
        .get("element_id")
        .and_then(Value::as_str)
        .ok_or(("invalid_request", "element_id is required.".to_string()))?;
    let mut args = base_args(observation);
    merge(&mut args, element_ref(observation, element_id)?);
    args.insert("value".to_string(), json!(value));
    Ok((request("set_value", Value::Object(args))?, None))
}

/// Milliseconds since the last keyboard or pointer event on the X server.
pub(crate) fn last_input_age_ms() -> Option<u32> {
    let reply = request("idle_ms", json!({})).ok()?;
    reply
        .as_u64()
        .map(|value| value.min(u64::from(u32::MAX)) as u32)
}

/// A virtual X display has no screen lock; a real session would ask the
/// screensaver service here.
pub(crate) fn desktop_locked() -> bool {
    false
}
