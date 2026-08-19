//! Wire-level input invariants shared by both native platform leaves.

use serde_json::{Map, Value};

// Ten standard Windows wheel detents and one bounded macOS pixel gesture.
const SCROLL_LIMIT: i64 = 1200;

/// Read an optional click count.
pub(super) fn click_count(params: &Map<String, Value>) -> Result<u64, (&'static str, String)> {
    let Some(value) = params.get("count") else {
        return Ok(1);
    };
    value
        .as_u64()
        .filter(|count| (1..=3).contains(count))
        .ok_or((
            "invalid_request",
            "count must be an integer between 1 and 3.".to_string(),
        ))
}

/// Convert the public positive-down distance to the native positive-up sign.
pub(super) fn native_scroll_delta(
    params: &Map<String, Value>,
) -> Result<i32, (&'static str, String)> {
    let value = params
        .get("delta_y")
        .and_then(Value::as_i64)
        .ok_or(("invalid_request", "delta_y is required.".to_string()))?;
    if value == 0 || !(-SCROLL_LIMIT..=SCROLL_LIMIT).contains(&value) {
        return Err((
            "invalid_request",
            format!("delta_y must be non-zero and between -{SCROLL_LIMIT} and {SCROLL_LIMIT}."),
        ));
    }
    Ok(-(value as i32))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn click_count_defaults_and_rejects_invalid_values() {
        assert_eq!(click_count(&Map::new()), Ok(1));
        for value in [1, 2, 3] {
            let params = json!({"count": value});
            assert_eq!(click_count(params.as_object().unwrap()), Ok(value));
        }
        for value in [
            json!(0),
            json!(4),
            json!(-1),
            json!(1.5),
            json!("2"),
            json!(true),
            json!(null),
        ] {
            let params = json!({"count": value});
            assert!(click_count(params.as_object().unwrap()).is_err());
        }
    }

    #[test]
    fn signed_scroll_delta_is_bounded_and_non_zero() {
        for (value, expected) in [(-1200, 1200), (-1, 1), (1, -1), (1200, -1200)] {
            let params = json!({"delta_y": value});
            assert_eq!(
                native_scroll_delta(params.as_object().unwrap()),
                Ok(expected),
            );
        }
        for value in [-1201, 0, 1201] {
            let params = json!({"delta_y": value});
            assert!(native_scroll_delta(params.as_object().unwrap()).is_err());
        }
    }
}
