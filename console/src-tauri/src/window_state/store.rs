//! Installation-scoped geometry, independent of WebView lifetime.

use std::{fs::File, io::Write, path::Path};

use fs2::FileExt;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct SavedState {
    version: u8,
    monitor_name: Option<String>,
    monitor_origin: [i32; 2],
    // Logical units relative to the monitor's work area, not global physical
    // pixels, so a DPI/layout change does not move a window off screen.
    offset: [f64; 2],
    size: [f64; 2],
    pub(super) maximized: bool,
}

#[derive(Clone, Debug)]
pub(super) struct Display {
    pub(super) name: Option<String>,
    pub(super) origin: [i32; 2],
    pub(super) work_origin: [i32; 2],
    pub(super) work_size: [u32; 2],
    pub(super) scale: f64,
}

#[derive(Debug, PartialEq)]
pub(super) struct Placement {
    pub(super) position: [i32; 2],
    pub(super) size: [u32; 2],
    pub(super) min_size: [f64; 2],
    pub(super) maximized: bool,
}

impl SavedState {
    pub(super) fn capture(
        position: [i32; 2],
        size: [u32; 2],
        maximized: bool,
        display: &Display,
    ) -> Option<Self> {
        let state = Self {
            version: 1,
            monitor_name: display.name.clone(),
            monitor_origin: display.origin,
            offset: [
                (f64::from(position[0]) - f64::from(display.work_origin[0])) / display.scale,
                (f64::from(position[1]) - f64::from(display.work_origin[1])) / display.scale,
            ],
            size: [
                f64::from(size[0]) / display.scale,
                f64::from(size[1]) / display.scale,
            ],
            maximized,
        };
        state.valid().then_some(state)
    }

    fn valid(&self) -> bool {
        self.version == 1
            && self.offset.iter().all(|value| value.is_finite())
            && self
                .size
                .iter()
                .all(|value| value.is_finite() && *value > 0.0)
    }

    pub(super) fn placement(
        &self,
        displays: &[Display],
        primary: usize,
        min_size: [f64; 2],
        frame: [f64; 2],
    ) -> Option<Placement> {
        if !self.valid() {
            return None;
        }
        // Names can repeat (two identical monitors). Prefer the same origin
        // among name matches; use the primary screen if that display vanished.
        let display = displays
            .iter()
            .filter(|display| {
                self.monitor_name.is_some() && display.name == self.monitor_name
                    || self.monitor_name.is_none() && display.origin == self.monitor_origin
            })
            .min_by_key(|display| {
                (i64::from(display.origin[0]) - i64::from(self.monitor_origin[0])).abs()
                    + (i64::from(display.origin[1]) - i64::from(self.monitor_origin[1])).abs()
            })
            .or_else(|| displays.get(primary))?;
        if !display.scale.is_finite() || display.scale <= 0.0 {
            return None;
        }
        let mut placement = Placement {
            position: [0; 2],
            size: [0; 2],
            min_size: [0.0; 2],
            maximized: self.maximized,
        };
        for axis in 0..2 {
            let decoration = (frame[axis].max(0.0) * display.scale).ceil();
            let available = f64::from(display.work_size[axis]) - decoration;
            if available < 1.0 {
                return None;
            }
            // A small screen must remain usable even if its work area is below
            // the configured minimum. Do not let the WM undo our clamping.
            let minimum = (min_size[axis] * display.scale).max(1.0).min(available);
            let size = (self.size[axis] * display.scale)
                .round()
                .clamp(minimum, available);
            let start = f64::from(display.work_origin[axis]);
            let end = start + available - size;
            placement.position[axis] = (start + self.offset[axis] * display.scale)
                .round()
                .clamp(start, end) as i32;
            placement.size[axis] = size as u32;
            placement.min_size[axis] = minimum / display.scale;
        }
        Some(placement)
    }
}

pub(super) fn read(directory: &Path) -> Result<Option<SavedState>, String> {
    let lock = lock_file(directory)?;
    FileExt::try_lock_shared(&lock).map_err(lock_error)?;
    match std::fs::read(directory.join("window-state.json")) {
        Ok(bytes) => {
            let state: SavedState =
                serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
            if !state.valid() {
                return Err("Invalid window state version or geometry".into());
            }
            Ok(Some(state))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

pub(super) fn write(directory: &Path, state: &SavedState) -> Result<(), String> {
    if !state.valid() {
        return Err("Invalid window state version or geometry".into());
    }
    let lock = lock_file(directory)?;
    lock.try_lock_exclusive().map_err(lock_error)?;
    // A private same-directory file avoids collisions between clients. Readers
    // take the same lock; a crash before rename leaves the previous state intact.
    let temporary = directory.join(format!("window-state-{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<(), String> {
        let mut file = File::options()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|error| error.to_string())?;
        file.write_all(&serde_json::to_vec(state).map_err(|error| error.to_string())?)
            .map_err(|error| error.to_string())?;
        file.sync_all().map_err(|error| error.to_string())?;
        drop(file);
        std::fs::rename(&temporary, directory.join("window-state.json"))
            .map_err(|error| error.to_string())?;
        #[cfg(unix)]
        File::open(directory)
            .and_then(|file| file.sync_all())
            .map_err(|error| error.to_string())?;
        Ok(())
    })();
    let _ = std::fs::remove_file(temporary);
    result
}

fn lock_file(directory: &Path) -> Result<File, String> {
    std::fs::create_dir_all(directory).map_err(|error| error.to_string())?;
    File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(directory.join("window-state.lock"))
        .map_err(|error| error.to_string())
}

fn lock_error(error: std::io::Error) -> String {
    if error.raw_os_error() == fs2::lock_contended_error().raw_os_error() {
        "Window geometry is busy in another desktop client".into()
    } else {
        error.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn display() -> Display {
        Display {
            name: Some("screen".into()),
            origin: [1920, 0],
            work_origin: [1920, 40],
            work_size: [1920, 1040],
            scale: 1.0,
        }
    }

    #[test]
    fn dpi_change_preserves_logical_normal_bounds_and_maximized_flag() {
        let state = SavedState::capture([2020, 140], [1000, 700], true, &display()).unwrap();
        let mut target = display();
        target.scale = 1.5;
        target.origin = [0, 0];
        target.work_origin = [0, 60];
        target.work_size = [3000, 1800];
        assert_eq!(
            state
                .placement(&[target], 0, [960.0, 600.0], [16.0, 39.0])
                .unwrap(),
            Placement {
                position: [150, 210],
                size: [1500, 1050],
                min_size: [960.0, 600.0],
                maximized: true
            }
        );
    }

    #[test]
    fn removed_monitor_clamps_the_actual_normal_bounds_to_primary_work_area() {
        let state = SavedState::capture([3500, 900], [1600, 1000], true, &display()).unwrap();
        let primary = Display {
            name: Some("laptop".into()),
            origin: [0, 0],
            work_origin: [0, 30],
            work_size: [800, 570],
            scale: 1.0,
        };
        assert_eq!(
            state
                .placement(&[primary], 0, [960.0, 600.0], [16.0, 39.0])
                .unwrap(),
            Placement {
                position: [0, 30],
                size: [784, 531],
                min_size: [784.0, 531.0],
                maximized: true
            }
        );
    }

    #[test]
    fn negative_coordinates_remain_valid_on_a_left_monitor() {
        let mut monitor = display();
        monitor.origin = [-1920, 0];
        monitor.work_origin = [-1920, 0];
        let state = SavedState::capture([-1800, 80], [1000, 700], false, &monitor).unwrap();
        let placement = state
            .placement(&[monitor], 0, [960.0, 600.0], [16.0, 39.0])
            .unwrap();
        assert_eq!(placement.position, [-1800, 80]);
        assert!(!placement.maximized);
    }

    #[test]
    fn invalid_version_or_nonfinite_geometry_never_restore() {
        let mut state = SavedState::capture([2020, 140], [1000, 700], false, &display()).unwrap();
        state.version = 2;
        assert!(state
            .placement(&[display()], 0, [1.0; 2], [0.0; 2])
            .is_none());
        state.version = 1;
        state.size[0] = f64::NAN;
        assert!(state
            .placement(&[display()], 0, [1.0; 2], [0.0; 2])
            .is_none());
    }

    #[test]
    fn missing_and_corrupt_state_have_explicit_results() {
        let directory =
            std::env::temp_dir().join(format!("qwenpaw-window-state-{}", uuid::Uuid::new_v4()));
        assert!(read(&directory).unwrap().is_none());
        std::fs::write(directory.join("window-state.json"), b"{").unwrap();
        assert!(read(&directory).is_err());
        let state = SavedState::capture([2020, 140], [1000, 700], true, &display()).unwrap();
        write(&directory, &state).unwrap();
        assert!(read(&directory).unwrap().unwrap().maximized);
        assert_eq!(std::fs::read_dir(&directory).unwrap().count(), 2);
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn duplicate_monitor_names_prefer_original_origin() {
        let state = SavedState::capture([2020, 140], [1000, 700], false, &display()).unwrap();
        let mut left = display();
        left.origin = [0, 0];
        left.work_origin = [0, 40];
        let placement = state
            .placement(&[left, display()], 0, [960.0, 600.0], [16.0, 39.0])
            .unwrap();
        assert_eq!(placement.position, [2020, 140]);
        assert!(state.placement(&[], 0, [1.0; 2], [0.0; 2]).is_none());
    }

    #[test]
    fn concurrent_clients_read_only_complete_committed_states() {
        let directory =
            std::env::temp_dir().join(format!("qwenpaw-window-state-{}", uuid::Uuid::new_v4()));
        let initial = SavedState::capture([2020, 140], [1000, 700], false, &display()).unwrap();
        write(&directory, &initial).unwrap();
        // A process interrupted before rename may leave a private temporary.
        // It must never replace or be read in preference to committed state.
        std::fs::write(directory.join("window-state-interrupted.tmp"), b"{").unwrap();
        std::thread::scope(|scope| {
            for width in [1000, 1100] {
                let directory = &directory;
                scope.spawn(move || {
                    for _ in 0..100 {
                        let state =
                            SavedState::capture([2020, 140], [width, 700], false, &display())
                                .unwrap();
                        if let Err(error) = write(directory, &state) {
                            assert_eq!(error, "Window geometry is busy in another desktop client");
                        }
                    }
                });
            }
            for _ in 0..2 {
                let directory = &directory;
                scope.spawn(move || {
                    for _ in 0..100 {
                        let state = match read(directory) {
                            Ok(Some(state)) => state,
                            Err(error) => {
                                assert_eq!(
                                    error,
                                    "Window geometry is busy in another desktop client"
                                );
                                continue;
                            }
                            Ok(None) => panic!("Committed state disappeared"),
                        };
                        assert!([1000.0, 1100.0].contains(&state.size[0]));
                        assert_eq!(state.size[1], 700.0);
                    }
                });
            }
        });
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn busy_geometry_never_waits_or_overwrites_previous_state() {
        let directory =
            std::env::temp_dir().join(format!("qwenpaw-window-state-{}", uuid::Uuid::new_v4()));
        let state = SavedState::capture([2020, 140], [1000, 700], false, &display()).unwrap();
        write(&directory, &state).unwrap();
        let before = std::fs::read(directory.join("window-state.json")).unwrap();
        let lock = lock_file(&directory).unwrap();
        lock.lock_exclusive().unwrap();
        let (sender, receiver) = std::sync::mpsc::channel();
        let target = directory.clone();
        let worker = std::thread::spawn(move || {
            let result = (read(&target), write(&target, &state));
            sender.send(result).unwrap();
        });
        let result = receiver.recv_timeout(std::time::Duration::from_secs(1));
        // Release before joining even on failure, so a blocking-lock regression
        // fails the test without leaving its worker hung forever.
        drop(lock);
        worker.join().unwrap();
        let (read_result, write_result) =
            result.expect("Busy geometry must not wait for its owner");
        assert_eq!(
            read_result.unwrap_err(),
            "Window geometry is busy in another desktop client"
        );
        assert_eq!(
            write_result.unwrap_err(),
            "Window geometry is busy in another desktop client"
        );
        assert_eq!(
            std::fs::read(directory.join("window-state.json")).unwrap(),
            before
        );
        std::fs::remove_dir_all(directory).unwrap();
    }
}
