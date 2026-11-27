//! Observation assembly and freshness checks.
//!
//! The platform process reads the accessibility tree and captures pixels; this
//! module turns its reply into the shared `Observation` and response shape the
//! Windows helper produces, so the model-facing contract stays the same.

use serde_json::{json, Value};
use std::collections::HashMap;

use super::super::state::{
    accessibility_revision, next_id, Observation, ScreenshotTarget, ServerState, WindowInfo,
};
use super::bridge::request;
use super::LinuxElement;

pub(crate) fn observe_window(
    state: &mut ServerState,
    window: &WindowInfo,
    include_screenshot: bool,
) -> Result<Value, (&'static str, String)> {
    let observation_id = next_id("observation");
    let reply = request(
        "observe",
        json!({
            "xid": window.hwnd,
            "key": observation_id,
            "screenshot": include_screenshot,
        }),
    )?;
    let accessibility = reply.get("accessibility").cloned().unwrap_or_else(|| {
        json!({"available": false, "reason": "Accessibility text was unavailable.", "elements": []})
    });
    let available = accessibility.get("available").and_then(Value::as_bool) == Some(true);
    let mut elements = HashMap::new();
    if let Some(published) = accessibility.get("elements").and_then(Value::as_array) {
        for (index, element) in published.iter().enumerate() {
            if let Some(id) = element.get("id").and_then(Value::as_str) {
                elements.insert(
                    id.to_string(),
                    LinuxElement {
                        key: observation_id.clone(),
                        index,
                    },
                );
            }
        }
    }
    let related_surface_count = reply
        .get("related_surface_count")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let mut targets = HashMap::new();
    let mut screenshots = Vec::new();
    let mut viewport = (0_u32, 0_u32);
    for capture in reply
        .get("screenshots")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        let Some(bounds) = bounds_of(capture.get("bounds")) else {
            continue;
        };
        let width = capture.get("width").and_then(Value::as_u64).unwrap_or(0) as u32;
        let height = capture.get("height").and_then(Value::as_u64).unwrap_or(0) as u32;
        let data = capture.get("data").and_then(Value::as_str).unwrap_or("");
        if width == 0 || height == 0 || data.is_empty() {
            continue;
        }
        let kind = capture.get("kind").and_then(Value::as_str).unwrap_or("main");
        let z_index = capture.get("z_index").and_then(Value::as_i64).unwrap_or(0);
        let hwnd = capture
            .get("xid")
            .and_then(Value::as_i64)
            .map(|value| value as isize)
            .unwrap_or(window.hwnd);
        let screenshot_id = next_id("screenshot");
        if kind == "main" {
            viewport = (width, height);
        }
        targets.insert(
            screenshot_id.clone(),
            ScreenshotTarget {
                hwnd,
                bounds,
                display_width: width,
                display_height: height,
            },
        );
        screenshots.push(json!({
            "id": screenshot_id,
            "url": format!("data:image/jpeg;base64,{data}"),
            "origin_x": bounds[0],
            "origin_y": bounds[1],
            "width": width,
            "height": height,
            "z_index": z_index,
            "kind": kind,
        }));
    }
    if include_screenshot && screenshots.is_empty() && !available {
        let capture_error = reply
            .get("visual_failure")
            .and_then(Value::as_str)
            .unwrap_or("No window image was captured.");
        let accessibility_error = accessibility
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("Accessibility text was unavailable.");
        return Err((
            "capture_failed",
            format!("{capture_error} Accessibility was also unavailable: {accessibility_error}"),
        ));
    }
    let visual = if !include_screenshot {
        json!({
            "available": false,
            "requested": false,
            "related_surface_count": related_surface_count,
        })
    } else if screenshots.is_empty() {
        json!({
            "available": false,
            "reason": reply
                .get("visual_failure")
                .and_then(Value::as_str)
                .unwrap_or("No window image was captured."),
        })
    } else {
        json!({"available": true})
    };
    let window_bounds = bounds_of(reply.get("bounds")).unwrap_or_default();
    state.observations.insert(
        observation_id.clone(),
        Observation {
            window: window.clone(),
            window_bounds,
            screenshots: targets,
            accessibility_revision: accessibility_revision(&accessibility),
            elements,
        },
    );
    Ok(json!({
        "observation_id": observation_id,
        "window": window.to_json(),
        "viewport": {"width": viewport.0, "height": viewport.1},
        "visual": visual,
        "accessibility": accessibility,
        "screenshots": screenshots,
    }))
}

/// Re-read the observed surface and refuse an action if it changed, the way
/// the Windows helper compares accessibility revisions.
pub(crate) fn validate_observation(
    observation: &Observation,
) -> Result<(), (&'static str, String)> {
    let expected = observation.accessibility_revision.ok_or((
        "stale_observation",
        "The observation had no accessibility revision; observe the window again.".to_string(),
    ))?;
    let reply = request("tree", json!({"xid": observation.window.hwnd})).map_err(|error| {
        (
            "stale_observation",
            format!("The observed accessibility surface is unavailable: {}", error.1),
        )
    })?;
    let current = reply.get("accessibility").cloned().unwrap_or_default();
    if accessibility_revision(&current) != Some(expected) {
        return Err((
            "stale_observation",
            "The observed window changed; observe it again before acting.".to_string(),
        ));
    }
    Ok(())
}

pub(super) fn bounds_of(value: Option<&Value>) -> Option<[i32; 4]> {
    let values = value?.as_array()?;
    if values.len() != 4 {
        return None;
    }
    let mut bounds = [0_i32; 4];
    for (slot, value) in bounds.iter_mut().zip(values) {
        *slot = i32::try_from(value.as_i64()?).ok()?;
    }
    Some(bounds)
}
