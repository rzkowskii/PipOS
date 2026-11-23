//! `brainstem --probe-hardware` answers "what did the body find?" and never
//! fails, whatever the machine looks like.

use std::path::{Path, PathBuf};
use std::process::Command;

struct Tree(PathBuf);

impl Tree {
    fn new(tag: &str) -> Tree {
        let root = std::env::temp_dir().join(format!("being-probe-{}-{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        Tree(root)
    }

    fn file(&self, path: &str, data: &str) {
        let path = self.0.join(path);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, data).unwrap();
    }
}

impl Drop for Tree {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn probe(root: &Path) -> String {
    let out = Command::new(env!("CARGO_BIN_EXE_brainstem"))
        .arg("--probe-hardware")
        .arg("--sysfs-root")
        .arg(root)
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn the_probe_reports_a_macbook_body() {
    let t = Tree::new("mbp");
    t.file("sys/class/power_supply/ADP1/type", "Mains\n");
    t.file("sys/class/power_supply/ADP1/online", "0\n");
    t.file("sys/class/power_supply/BAT0/type", "Battery\n");
    t.file("sys/class/power_supply/BAT0/status", "Discharging\n");
    t.file("sys/class/power_supply/BAT0/capacity", "64\n");
    t.file("sys/class/hwmon/hwmon3/name", "coretemp\n");
    t.file("sys/class/hwmon/hwmon3/temp1_input", "48000\n");
    t.file("sys/devices/platform/applesmc.768/light", "(0,0)\n");
    t.file("sys/class/backlight/intel_backlight/type", "raw\n");
    t.file("sys/class/backlight/intel_backlight/bl_power", "0\n");
    t.file("sys/class/input/event2/device/name", "Lid Switch\n");
    t.file("sys/class/input/event2/device/capabilities/sw", "1\n");
    let out = probe(&t.0);
    for want in [
        "lid: found \"Lid Switch\" (event2)",
        "power: found battery BAT0",
        "power: found AC adapter ADP1",
        "backlight: intel_backlight (raw)",
        "light sensor: found, raw 0",
        "reading: battery 64%, charging no, on AC no, CPU 48 C, light 0 lux (approx.)",
    ] {
        assert!(out.contains(want), "missing {want:?} in\n{out}");
    }
}

#[test]
fn the_probe_of_an_empty_machine_still_answers() {
    let t = Tree::new("empty");
    let out = probe(&t.0);
    assert!(out.contains("no lid switch found"), "{out}");
    assert!(out.contains("reading: battery unknown, charging unknown, on AC unknown, CPU unknown, light unknown"), "{out}");
    let missing = probe(&t.0.join("does-not-exist"));
    assert!(missing.contains("reading: battery unknown"), "{missing}");
}
