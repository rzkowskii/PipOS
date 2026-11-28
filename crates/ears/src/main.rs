//! Local PCM capture organ. The device implementation is Linux-only.

#[cfg(target_os = "linux")]
mod linux;

#[cfg(target_os = "linux")]
fn main() -> std::io::Result<()> {
    linux::run()
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("being-ears needs Linux ALSA and systemd peer credentials");
    std::process::exit(2);
}
