//! Kernel-backed microphone owner. Raw PCM goes only over a local socket to
//! the authenticated voice unit; the event bus carries policy, never audio.

use bus::{socket_path, Client};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const SOCKET: &str = "/run/being/ears.sock";
const VOICE_UNIT: &str = "being-voice.service";
const MIC_POLICY_LATCH: &str = "/run/being/mic-policy-breach";

fn unit_from_cgroup(text: &str) -> Option<&str> {
    let line = text.lines().find(|line| line.starts_with("0::"))
        .or_else(|| text.lines().find(|line| line.contains(":name=systemd:")))?;
    line.rsplit_once(':')?.1.split('/').rev().find(|part| part.ends_with(".service"))
}

fn voice_peer(stream: &UnixStream) -> bool {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let status = unsafe {
        libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(), &mut len)
    };
    status == 0 && cred.pid > 0
        && std::fs::read_to_string(format!("/proc/{}/cgroup", cred.pid)).ok()
            .and_then(|text| unit_from_cgroup(&text).map(str::to_owned)).as_deref() == Some(VOICE_UNIT)
}

fn nonblocking(fd: i32) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

struct Capture {
    child: Child,
    output: ChildStdout,
    overruns: Arc<AtomicU64>,
    stderr: Option<std::thread::JoinHandle<()>>,
}

impl Capture {
    fn start(channels: u8) -> io::Result<Self> {
        let lab_loopback = Path::new("/sys/firmware/qemu_fw_cfg/by_name/opt/beingos/loopback/raw").exists();
        let device = if let Some((card, pcm)) = bus::body_map::at(Path::new("/")).mic {
            // Brainstem checks this same PCM for a closed-policy breach.
            format!("plughw:CARD={card},DEV={pcm}")
        } else {
            std::env::var("BEING_ALSA_CAPTURE").unwrap_or_else(|_| {
                if lab_loopback { "hw:Loopback,1,0".into() } else { "plughw:0,0".into() }
            })
        };
        let channels = channels.to_string();
        let mut child = Command::new("arecord").args([
            "-D", &device, "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", &channels,
            "--buffer-time=500000", "--period-time=30000",
        ]).env("LC_ALL", "C").stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
        let (Some(output), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::other("capture pipes were not created"));
        };
        if let Err(error) = nonblocking(output.as_raw_fd()) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        let overruns = Arc::new(AtomicU64::new(0));
        let counted = Arc::clone(&overruns);
        let stderr = std::thread::spawn(move || {
            // arecord localises this line; LC_ALL=C above makes the marker
            // stable. Only the count survives this process, never its text.
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                if line.contains("overrun!!!") {
                    counted.fetch_add(1, Ordering::Relaxed);
                }
            }
        });
        Ok(Self { child, output, overruns, stderr: Some(stderr) })
    }

    fn stop(&mut self) {
        let Some(stderr) = self.stderr.take() else { return };
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = stderr.join();
    }
}

impl Drop for Capture {
    fn drop(&mut self) { self.stop(); }
}

fn receive_policy(bus: &Client, allowed: &mut bool, last_beat: &mut Instant) -> io::Result<bool> {
    if Path::new(MIC_POLICY_LATCH).exists() {
        *allowed = false;
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "microphone safety latch is active"));
    }
    let mut revoked = false;
    loop {
        match bus.try_recv() {
            Ok(Some(ev)) if ev.topic == "ears.policy" => {
                let current = ev.bool("allowed") == Some(true);
                revoked |= *allowed && !current;
                *allowed = current;
            }
            Ok(Some(ev)) if ev.topic == "stem.beat" => *last_beat = Instant::now(),
            Ok(Some(_)) => {}
            Ok(None) => break,
            Err(_) => return Err(io::Error::new(io::ErrorKind::ConnectionReset, "brainstem disconnected")),
        }
    }
    // A connected but wedged brainstem is no authority to keep listening.
    if last_beat.elapsed() > Duration::from_secs(2) {
        *allowed = false;
        return Err(io::Error::new(io::ErrorKind::TimedOut, "brainstem heartbeat stopped"));
    }
    Ok(revoked)
}

fn serve(mut voice: UnixStream, bus: &mut Client, allowed: &mut bool, last_beat: &mut Instant) -> io::Result<()> {
    // Voice declares the raw stream shape before the device opens. This keeps
    // the laptop's stereo channel picker and the lab/explicit mono mixer exact.
    voice.set_read_timeout(Some(Duration::from_secs(1)))?;
    let mut request = [0u8; 1];
    voice.read_exact(&mut request)?;
    let channels = match request[0] {
        1 | 2 => request[0],
        _ => return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid channel count")),
    };
    if receive_policy(bus, allowed, last_beat)? || !*allowed {
        return Ok(());
    }
    voice.set_write_timeout(Some(Duration::from_millis(100)))?;
    let mut capture = Capture::start(channels)?;
    bus.send(&bus::Event::new("ears.capture").with("overruns", 0).with("active", true))?;
    let mut reported = 0;
    let mut pcm = [0u8; 16 * 1024];
    let mut streams = [
        libc::pollfd { fd: capture.output.as_raw_fd(), events: libc::POLLIN, revents: 0 },
        libc::pollfd { fd: voice.as_raw_fd(), events: libc::POLLIN | libc::POLLRDHUP, revents: 0 },
    ];
    let result = (|| {
        while *allowed {
            if receive_policy(bus, allowed, last_beat)? || !*allowed {
                break;
            }
            let count = capture.overruns.load(Ordering::Relaxed);
            if count != reported {
                reported = count;
                bus.send(&bus::Event::new("ears.capture").with("overruns", count).with("active", true))?;
            }
            let ready = unsafe { libc::poll(streams.as_mut_ptr(), streams.len() as libc::nfds_t, 40) };
            if ready < 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(error);
            }
            if streams[1].revents != 0 {
                break;
            }
            if streams[0].revents == 0 {
                continue;
            }
            match capture.output.read(&mut pcm) {
                Ok(0) => {
                    if let Ok(Some(status)) = capture.child.try_wait() {
                        if !status.success() {
                            eprintln!("ears: microphone capture exited with {status}");
                        }
                    }
                    break;
                }
                Ok(n) => voice.write_all(&pcm[..n])?,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Err(error),
            }
        }
        Ok(())
    })();
    capture.stop();
    let count = capture.overruns.load(Ordering::Relaxed);
    let _ = bus.send(&bus::Event::new("ears.capture").with("overruns", count).with("active", false));
    result
}

pub fn run() -> io::Result<()> {
    let socket = PathBuf::from(std::env::var_os("BEING_EARS_SOCKET").unwrap_or_else(|| SOCKET.into()));
    if let Some(parent) = socket.parent() { std::fs::create_dir_all(parent)?; }
    let _ = std::fs::remove_file(&socket);
    let listener = UnixListener::bind(&socket)?;
    std::fs::set_permissions(&socket, std::os::unix::fs::PermissionsExt::from_mode(0o660))?;
    listener.set_nonblocking(true)?;
    let bus_path = std::env::var_os("BEING_BUS").map(PathBuf::from).unwrap_or_else(socket_path);
    let mut bus = Client::connect_joined(Path::new(&bus_path), "ears", &["ears.policy", "stem.beat"], Duration::from_secs(2))?;
    let mut allowed = false;
    let mut last_beat = Instant::now();
    loop {
        receive_policy(&bus, &mut allowed, &mut last_beat)?;
        match listener.accept() {
            Ok((stream, _)) if voice_peer(&stream) && allowed => {
                if let Err(error) = serve(stream, &mut bus, &mut allowed, &mut last_beat) {
                    if error.kind() == io::ErrorKind::TimedOut { return Err(error); }
                }
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => std::thread::sleep(Duration::from_millis(10)),
            Err(error) => return Err(error),
        }
    }
}
