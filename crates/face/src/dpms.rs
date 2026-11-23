//! Turning the panel's connector off while the face is dark (task 2.2).
//!
//! bl_power does nothing on the MacBook's LVDS panel, so a black frame alone
//! leaves the backlight lit all night. The face holds the DRM master through
//! SDL's KMS/DRM driver, so it sets the connector's legacy "DPMS" property on
//! SDL's own file descriptor: off once the black frame has been shown for a
//! moment, and on again before the first frame of the wake gesture.
//!
//! Anywhere else (a window on a Mac, an SDL without KMS/DRM, a driver with no
//! DPMS property) this does nothing and says so once; the brainstem's own
//! backlight handling still applies.

use std::time::{Duration, Instant};

/// How long the black frame stays on scanout before the connector goes off,
/// so a lid that bounces does not flicker the panel.
pub const OFF_AFTER: Duration = Duration::from_millis(1500);

/// What the loop should do with the connector this frame.
#[derive(Debug, PartialEq, Eq)]
pub enum Step {
    Nothing,
    TurnOff,
    TurnOn,
}

/// The connector's power, decided from whether the face is dark.
pub struct Power {
    enabled: bool,
    off: bool,
    dark_since: Option<Instant>,
}

impl Power {
    pub fn new(enabled: bool) -> Power {
        Power { enabled, off: false, dark_since: None }
    }

    pub fn disable(&mut self) {
        self.enabled = false;
    }

    pub fn is_off(&self) -> bool {
        self.off
    }

    /// Before drawing: `dark` is whether the panel is blank and its black
    /// frame has already been presented.
    pub fn step(&mut self, now: Instant, blank: bool, dark: bool) -> Step {
        if !blank {
            self.dark_since = None;
            if self.off {
                self.off = false;
                return Step::TurnOn;
            }
            return Step::Nothing;
        }
        if !dark || !self.enabled || self.off {
            return Step::Nothing;
        }
        let since = *self.dark_since.get_or_insert(now);
        if now.saturating_duration_since(since) >= OFF_AFTER {
            self.off = true;
            return Step::TurnOff;
        }
        Step::Nothing
    }

    /// On the way out: the connector must never stay off behind a face that
    /// is gone.
    pub fn restore(&mut self) -> bool {
        std::mem::replace(&mut self.off, false)
    }
}

/// The DRM file descriptor SDL's KMS/DRM driver draws through, if that is
/// the driver in use.
pub fn sdl_drm_fd(window: &sdl2::video::Window) -> Option<i32> {
    // SDL_SysWMinfo is opaque in the bindings: version (3 bytes), then the
    // subsystem as a 32-bit enum at offset 4, then the union at offset 8.
    // The KMS/DRM member is { int dev_index; int drm_fd; struct gbm_device * }.
    const SDL_SYSWM_KMSDRM: u32 = 13;
    let mut info = std::mem::MaybeUninit::<sdl2::sys::SDL_SysWMinfo>::zeroed();
    unsafe {
        let mut version = std::mem::zeroed::<sdl2::sys::SDL_version>();
        sdl2::sys::SDL_GetVersion(&mut version);
        let bytes = info.as_mut_ptr().cast::<u8>();
        *bytes = version.major;
        *bytes.add(1) = version.minor;
        *bytes.add(2) = version.patch;
        if sdl2::sys::SDL_GetWindowWMInfo(window.raw(), info.as_mut_ptr()) != sdl2::sys::SDL_bool::SDL_TRUE {
            return None;
        }
        let words = info.as_ptr().cast::<u32>();
        if *words.add(1) != SDL_SYSWM_KMSDRM {
            return None;
        }
        let fd = *words.add(3) as i32;
        (fd >= 0).then_some(fd)
    }
}

/// `_IOWR('d', nr, size)` as Linux encodes it.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub const fn drm_iowr(nr: u32, size: u32) -> u32 {
    (3 << 30) | (size << 16) | ((b'd' as u32) << 8) | nr
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
mod numbers {
    use super::drm_iowr;
    pub const GETRESOURCES: u32 = drm_iowr(0xA0, 64);
    pub const GETPROPERTY: u32 = drm_iowr(0xAA, 64);
    pub const OBJ_GETPROPERTIES: u32 = drm_iowr(0xB9, 32);
    pub const OBJ_SETPROPERTY: u32 = drm_iowr(0xBA, 24);
    pub const OBJECT_CONNECTOR: u32 = 0xc0c0_c0c0;
}
#[cfg_attr(not(target_os = "linux"), allow(unused_imports))]
use numbers::*;
pub const DPMS_ON: u64 = 0;
pub const DPMS_OFF: u64 = 3;

/// Every connector and its DPMS property id, found once.
#[derive(Debug, Default)]
pub struct Connectors {
    found: Option<Vec<(u32, u32)>>,
}

impl Connectors {
    /// Set every connector's DPMS. Returns how many took it.
    pub fn set(&mut self, fd: i32, value: u64) -> Result<usize, String> {
        if self.found.is_none() {
            self.found = Some(drm::dpms_connectors(fd)?);
        }
        let list = self.found.as_deref().unwrap_or_default();
        if list.is_empty() {
            return Err("no connector has a DPMS property".to_string());
        }
        let mut done = 0;
        let mut last = String::new();
        for &(connector, prop) in list {
            match drm::set_property(fd, connector, prop, value) {
                Ok(()) => done += 1,
                Err(e) => last = e,
            }
        }
        if done == 0 {
            Err(last)
        } else {
            Ok(done)
        }
    }
}

#[cfg(target_os = "linux")]
mod drm {
    use std::os::raw::{c_int, c_ulong};

    extern "C" {
        fn ioctl(fd: c_int, request: c_ulong, ...) -> c_int;
    }

    #[repr(C)]
    #[derive(Default)]
    struct CardRes {
        fb_id_ptr: u64,
        crtc_id_ptr: u64,
        connector_id_ptr: u64,
        encoder_id_ptr: u64,
        count_fbs: u32,
        count_crtcs: u32,
        count_connectors: u32,
        count_encoders: u32,
        min_width: u32,
        max_width: u32,
        min_height: u32,
        max_height: u32,
    }

    #[repr(C)]
    #[derive(Default)]
    struct ObjGetProperties {
        props_ptr: u64,
        prop_values_ptr: u64,
        count_props: u32,
        obj_id: u32,
        obj_type: u32,
        pad: u32,
    }

    #[repr(C)]
    struct GetProperty {
        values_ptr: u64,
        enum_blob_ptr: u64,
        prop_id: u32,
        flags: u32,
        name: [u8; 32],
        count_values: u32,
        count_enum_blobs: u32,
    }

    #[repr(C)]
    struct ObjSetProperty {
        value: u64,
        prop_id: u32,
        obj_id: u32,
        obj_type: u32,
        pad: u32,
    }

    const _: () = assert!(std::mem::size_of::<CardRes>() == 64);
    const _: () = assert!(std::mem::size_of::<ObjGetProperties>() == 32);
    const _: () = assert!(std::mem::size_of::<GetProperty>() == 64);
    const _: () = assert!(std::mem::size_of::<ObjSetProperty>() == 24);

    fn call<T>(fd: i32, request: u32, arg: &mut T, what: &str) -> Result<(), String> {
        loop {
            let rc = unsafe { ioctl(fd, request as c_ulong, arg as *mut T) };
            if rc == 0 {
                return Ok(());
            }
            let e = std::io::Error::last_os_error();
            if e.kind() != std::io::ErrorKind::Interrupted {
                return Err(format!("{what}: {e}"));
            }
        }
    }

    pub fn dpms_connectors(fd: i32) -> Result<Vec<(u32, u32)>, String> {
        let mut res = CardRes::default();
        call(fd, super::GETRESOURCES, &mut res, "DRM resources")?;
        let mut ids = vec![0u32; res.count_connectors as usize];
        let mut res = CardRes {
            connector_id_ptr: ids.as_mut_ptr() as u64,
            count_connectors: ids.len() as u32,
            ..CardRes::default()
        };
        call(fd, super::GETRESOURCES, &mut res, "DRM connectors")?;
        ids.truncate(res.count_connectors as usize);
        let mut found = Vec::new();
        for connector in ids {
            let mut list = ObjGetProperties { obj_id: connector, obj_type: super::OBJECT_CONNECTOR, ..Default::default() };
            call(fd, super::OBJ_GETPROPERTIES, &mut list, "connector properties")?;
            let mut props = vec![0u32; list.count_props as usize];
            let mut values = vec![0u64; list.count_props as usize];
            let mut list = ObjGetProperties {
                props_ptr: props.as_mut_ptr() as u64,
                prop_values_ptr: values.as_mut_ptr() as u64,
                count_props: props.len() as u32,
                obj_id: connector,
                obj_type: super::OBJECT_CONNECTOR,
                pad: 0,
            };
            call(fd, super::OBJ_GETPROPERTIES, &mut list, "connector properties")?;
            props.truncate(list.count_props as usize);
            for prop in props {
                let mut info = GetProperty {
                    values_ptr: 0,
                    enum_blob_ptr: 0,
                    prop_id: prop,
                    flags: 0,
                    name: [0; 32],
                    count_values: 0,
                    count_enum_blobs: 0,
                };
                if call(fd, super::GETPROPERTY, &mut info, "property").is_ok() && super::is_dpms(&info.name) {
                    found.push((connector, prop));
                }
            }
        }
        Ok(found)
    }

    pub fn set_property(fd: i32, connector: u32, prop: u32, value: u64) -> Result<(), String> {
        let mut set = ObjSetProperty { value, prop_id: prop, obj_id: connector, obj_type: super::OBJECT_CONNECTOR, pad: 0 };
        call(fd, super::OBJ_SETPROPERTY, &mut set, "set DPMS")
    }
}

#[cfg(not(target_os = "linux"))]
mod drm {
    pub fn dpms_connectors(_fd: i32) -> Result<Vec<(u32, u32)>, String> {
        Err("DPMS needs Linux KMS/DRM".to_string())
    }

    pub fn set_property(_fd: i32, _connector: u32, _prop: u32, _value: u64) -> Result<(), String> {
        Err("DPMS needs Linux KMS/DRM".to_string())
    }
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn is_dpms(name: &[u8; 32]) -> bool {
    let end = name.iter().position(|&b| b == 0).unwrap_or(name.len());
    &name[..end] == b"DPMS"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ioctl_numbers_match_the_kernel_headers() {
        assert_eq!(GETRESOURCES, 0xC040_64A0);
        assert_eq!(GETPROPERTY, 0xC040_64AA);
        assert_eq!(OBJ_GETPROPERTIES, 0xC020_64B9);
        assert_eq!(OBJ_SETPROPERTY, 0xC018_64BA);
    }

    #[test]
    fn the_property_is_found_by_its_exact_name() {
        let mut name = [0u8; 32];
        name[..4].copy_from_slice(b"DPMS");
        assert!(is_dpms(&name));
        name[4] = b'X';
        assert!(!is_dpms(&name));
    }

    #[test]
    fn the_connector_goes_off_only_after_the_black_frame_has_rested() {
        let start = Instant::now();
        let mut power = Power::new(true);
        assert_eq!(power.step(start, true, false), Step::Nothing, "the black frame is not up yet");
        assert_eq!(power.step(start, true, true), Step::Nothing);
        assert_eq!(power.step(start + Duration::from_millis(1000), true, true), Step::Nothing);
        assert_eq!(power.step(start + OFF_AFTER, true, true), Step::TurnOff);
        assert!(power.is_off());
        assert_eq!(power.step(start + Duration::from_secs(9), true, true), Step::Nothing);
        // The lid opens: on before anything is drawn.
        assert_eq!(power.step(start + Duration::from_secs(10), false, false), Step::TurnOn);
        assert_eq!(power.step(start + Duration::from_secs(10), false, false), Step::Nothing);
        // A short dark spell starts the wait again.
        assert_eq!(power.step(start + Duration::from_secs(11), true, true), Step::Nothing);
        assert_eq!(power.step(start + Duration::from_secs(12), false, false), Step::Nothing);
    }

    #[test]
    fn a_disabled_connector_is_never_touched_and_exit_restores_it() {
        let start = Instant::now();
        let mut off = Power::new(false);
        assert_eq!(off.step(start + Duration::from_secs(60), true, true), Step::Nothing);
        let mut power = Power::new(true);
        power.step(start, true, true);
        assert_eq!(power.step(start + OFF_AFTER, true, true), Step::TurnOff);
        assert!(power.restore());
        assert!(!power.restore());
    }
}
