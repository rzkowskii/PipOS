//! The hardware locations known to the appliance. An unknown machine uses
//! an unplaced glance and reports no internal camera authorisation.

use serde_json::Value;
use std::path::Path;
use std::sync::OnceLock;

#[derive(Clone, Copy)]
pub struct BodyMap {
    pub usb_storage_place: &'static str,
    pub camera: Option<(&'static str, &'static str)>,
    pub mic: Option<(&'static str, u32)>,
}

const MAPS: &str = include_str!("../../../config/body-maps.json");

pub fn at(root: &Path) -> BodyMap {
    static KNOWN: OnceLock<Value> = OnceLock::new();
    let known = KNOWN.get_or_init(|| serde_json::from_str(MAPS).unwrap_or(Value::Null));
    let name = std::fs::read_to_string(root.join("sys/class/dmi/id/product_name")).unwrap_or_default();
    let entry = &known[name.trim()];
    let usb_storage_place = match entry["usb_storage_place"].as_str() {
        Some("left") => "left",
        _ => "nearby",
    };
    let camera = &entry["internal_camera"];
    let mic = &entry["internal_mic"];
    BodyMap {
        usb_storage_place,
        camera: camera["vendor"].as_str().zip(camera["product"].as_str()),
        mic: mic["card_id"].as_str().zip(mic["device"].as_u64().and_then(|n| u32::try_from(n).ok())),
    }
}
