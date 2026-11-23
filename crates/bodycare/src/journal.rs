//! The restore journal: every control's value from before bodycare first
//! changed it, written down *before* the change. `--restore` (systemd's
//! ExecStopPost) puts them back even if the controller was killed, so a
//! crash never leaves the machine with a changed setting nobody owns.
//!
//! The file lives in /run (memory): the unit's own runtime directory when
//! systemd gives it one ($RUNTIME_DIRECTORY), which stays writable under
//! ProtectSystem=strict. A reboot resets the volatile controls
//! (fan floor, P-state, RAPL) by itself, so a lost journal loses nothing.
//! The charge limit is different: it lives in the SMC and survives power
//! off. That is the point of it, so restoring it is a separate, explicit
//! choice (`--restore --charge-limit-too`).

use crate::hw::write_value;
use serde_json::{Map, Value};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// The kind of control, which decides whether a plain restore undoes it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    FanMin,
    NoTurbo,
    MaxPerf,
    RaplPl1,
    ChargeLimit,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::FanMin => "fan_min",
            Kind::NoTurbo => "no_turbo",
            Kind::MaxPerf => "max_perf_pct",
            Kind::RaplPl1 => "rapl_pl1",
            Kind::ChargeLimit => "charge_limit",
        }
    }

    fn from_name(name: &str) -> Option<Kind> {
        [Kind::FanMin, Kind::NoTurbo, Kind::MaxPerf, Kind::RaplPl1, Kind::ChargeLimit].into_iter().find(|k| k.name() == name)
    }
}

pub struct Journal {
    path: PathBuf,
    /// path -> {kind, value}
    entries: Map<String, Value>,
}

impl Journal {
    /// Open (or start) the journal at `path`. A damaged file is kept aside,
    /// never trusted and never silently lost.
    pub fn open(path: &Path) -> Journal {
        let entries = match fs::read_to_string(path) {
            Ok(text) => match serde_json::from_str::<Value>(&text) {
                Ok(Value::Object(map)) => map,
                _ => {
                    let mut aside = path.as_os_str().to_owned();
                    aside.push(".damaged");
                    let _ = fs::rename(path, PathBuf::from(aside));
                    Map::new()
                }
            },
            Err(_) => Map::new(),
        };
        Journal { path: path.to_path_buf(), entries }
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn original(&self, control: &Path) -> Option<String> {
        self.entries.get(&control.to_string_lossy().into_owned())?.get("value")?.as_str().map(str::to_string)
    }

    fn save(&self) -> io::Result<()> {
        if self.entries.is_empty() {
            return match fs::remove_file(&self.path) {
                Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
                _ => Ok(()),
            };
        }
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir)?;
        }
        let mut tmp = self.path.as_os_str().to_owned();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        fs::write(&tmp, Value::Object(self.entries.clone()).to_string())?;
        fs::rename(&tmp, &self.path)
    }

    /// Can the journal be written where it lives? Act mode asks at start,
    /// so a read-only location is said plainly instead of every pass.
    pub fn check_writable(&self) -> io::Result<()> {
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir)?;
        }
        let mut probe = self.path.as_os_str().to_owned();
        probe.push(".probe");
        let probe = PathBuf::from(probe);
        fs::write(&probe, b"")?;
        fs::remove_file(&probe)
    }

    /// Note a control's original value before its first change. Already
    /// noted controls keep their first value. An error means: do not write.
    pub fn remember(&mut self, control: &Path, kind: Kind, original: &str) -> io::Result<()> {
        let key = control.to_string_lossy().into_owned();
        if self.entries.contains_key(&key) {
            return Ok(());
        }
        let mut entry = Map::new();
        entry.insert("kind".into(), kind.name().into());
        entry.insert("value".into(), original.into());
        self.entries.insert(key.clone(), Value::Object(entry));
        if let Err(e) = self.save() {
            self.entries.remove(&key);
            return Err(e);
        }
        Ok(())
    }

    /// Put every noted control back. The charge limit only when asked.
    /// Returns one line per control, for the log.
    pub fn restore(&mut self, charge_limit_too: bool) -> Vec<String> {
        let mut lines = Vec::new();
        let keys: Vec<String> = self.entries.keys().cloned().collect();
        for key in keys {
            let entry = &self.entries[&key];
            let kind = entry.get("kind").and_then(Value::as_str).and_then(Kind::from_name);
            let Some(value) = entry.get("value").and_then(Value::as_str).map(str::to_string) else {
                self.entries.remove(&key);
                continue;
            };
            if kind == Some(Kind::ChargeLimit) && !charge_limit_too {
                lines.push(format!("kept the charge limit; {key} was {value} before (--restore --charge-limit-too undoes it)"));
                continue;
            }
            match write_value(Path::new(&key), &value) {
                Ok(()) => {
                    lines.push(format!("restored {key} to {value}"));
                    self.entries.remove(&key);
                }
                // A control that has gone (driver unloaded) needs nothing.
                Err(e) if e.kind() == io::ErrorKind::NotFound => {
                    lines.push(format!("{key} has gone; nothing to restore"));
                    self.entries.remove(&key);
                }
                Err(e) => lines.push(format!("could not restore {key} to {value}: {e}")),
            }
        }
        if let Err(e) = self.save() {
            lines.push(format!("could not update the journal {}: {e}", self.path.display()));
        }
        lines
    }
}
