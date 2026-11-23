//! The real binary on a fixture tree: one observe pass, an act pass, and the
//! ExecStopPost restore.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn tree(tag: &str) -> PathBuf {
    let root = std::env::temp_dir().join(format!("bodycare-cli-{}-{tag}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    for (path, data) in [
        ("sys/class/power_supply/ADP1/type", "Mains"),
        ("sys/class/power_supply/ADP1/online", "0"),
        ("sys/class/power_supply/BAT0/type", "Battery"),
        ("sys/class/power_supply/BAT0/capacity", "55"),
        ("sys/class/power_supply/BAT0/status", "Discharging"),
        ("sys/devices/platform/applesmc.768/fan1_min", "2000"),
        ("sys/devices/platform/applesmc.768/fan1_max", "6200"),
        ("sys/devices/platform/applesmc.768/fan1_manual", "0"),
        ("sys/class/hwmon/hwmon0/name", "coretemp"),
        ("sys/class/hwmon/hwmon0/temp1_label", "Package id 0"),
        ("sys/class/hwmon/hwmon0/temp1_input", "88000"),
        ("sys/devices/system/cpu/intel_pstate/no_turbo", "0"),
        ("sys/devices/system/cpu/intel_pstate/max_perf_pct", "100"),
    ] {
        let p = root.join(path);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, format!("{data}\n")).unwrap();
    }
    root
}

fn get(root: &Path, path: &str) -> String {
    fs::read_to_string(root.join(path)).unwrap().trim().to_string()
}

fn run(root: &Path, args: &[&str]) -> (bool, String, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_bodycare"))
        .arg("--root")
        .arg(root)
        .arg("--journal")
        .arg(root.join("journal.json"))
        .args(args)
        .env_remove("BEING_BODYCARE_MODE")
        .env_remove("BEING_CHARGE_LIMIT")
        .env_remove("NOTIFY_SOCKET")
        .output()
        .unwrap();
    (out.status.success(), String::from_utf8_lossy(&out.stdout).into_owned(), String::from_utf8_lossy(&out.stderr).into_owned())
}

#[test]
fn observe_reports_and_writes_nothing_then_act_and_restore_round_trip() {
    let root = tree("round");
    let (ok, report, log) = run(&root, &["--once"]);
    assert!(ok, "{log}");
    let report: serde_json::Value = serde_json::from_str(report.trim()).unwrap();
    assert_eq!(report["t"], "body.care");
    assert_eq!(report["mode"], "observe");
    assert_eq!(report["no_turbo"], true, "on battery");
    assert!(log.contains("would set no_turbo from 0 to 1"), "{log}");
    assert_eq!(get(&root, "sys/devices/system/cpu/intel_pstate/no_turbo"), "0");
    assert!(!root.join("journal.json").exists());

    let (ok, _, log) = run(&root, &["--once", "--mode", "act"]);
    assert!(ok, "{log}");
    assert_eq!(get(&root, "sys/devices/system/cpu/intel_pstate/no_turbo"), "1");
    assert_eq!(get(&root, "sys/devices/platform/applesmc.768/fan1_min"), "2250");
    assert_eq!(get(&root, "sys/devices/platform/applesmc.768/fan1_manual"), "0");

    let (ok, _, log) = run(&root, &["--restore"]);
    assert!(ok, "{log}");
    assert_eq!(get(&root, "sys/devices/system/cpu/intel_pstate/no_turbo"), "0");
    assert_eq!(get(&root, "sys/devices/platform/applesmc.768/fan1_min"), "2000");
    let (ok, _, log) = run(&root, &["--restore"]);
    assert!(ok && log.contains("nothing to restore"), "{log}");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn bad_settings_are_refused_loudly() {
    let root = tree("bad");
    let (ok, _, log) = run(&root, &["--charge-limit", "30"]);
    assert!(!ok && log.contains("60-100"), "{log}");
    let (ok, _, _) = run(&root, &["--mode", "maybe"]);
    assert!(!ok);
    let _ = fs::remove_dir_all(&root);
}
