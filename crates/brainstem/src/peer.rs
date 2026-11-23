//! Who is on the other end of a bus connection.
//!
//! On the appliance every organ runs in its own systemd unit, so the kernel's
//! peer credentials (SO_PEERCRED) name the process, and its cgroup names the
//! unit. An organ's name on the bus is then checked against the unit it runs
//! in: only being-mind.service may call itself "mind". Elsewhere (a
//! development Mac, host tests, a brainstem not run by systemd) nothing can be
//! learnt this way and names are taken as given, as before.

use std::os::unix::net::UnixStream;

/// Organ names that only their own units may use.
pub const PROTECTED: &[(&str, &[&str])] = &[
    ("face", &["being-face.service", "being-local-screen.service"]),
    ("mind", &["being-mind.service"]),
    // The lab's simulated body stands in for the voice in the QEMU lab.
    ("voice", &["being-voice.service", "being-lab-body.service"]),
    ("eyes", &["being-eyes.service"]),
    ("hands", &["being-hands.service"]),
    ("bodycare", &["being-bodycare.service"]),
    // The typing chat (mind/type_chat.py) runs from the local menu, and its
    // words count as the owner's, so no other unit may take its name.
    ("keyboard", &["being-local-screen.service"]),
];

/// The unit our own process runs in, if it is the appliance brainstem.
pub const BRAINSTEM_UNIT: &str = "being-brainstem.service";

/// The systemd unit in a /proc/PID/cgroup text: the innermost path
/// component ending in `.service` on the unified (`0::`) line.
pub fn unit_from_cgroup(text: &str) -> Option<String> {
    let line = text.lines().find(|l| l.starts_with("0::")).or_else(|| text.lines().find(|l| l.contains(":name=systemd:")))?;
    let path = line.rsplit_once(':')?.1;
    path.split('/').rev().find(|part| part.ends_with(".service")).map(str::to_string)
}

/// May a peer in `unit` (None: unknown) call itself `name`?
pub fn may_claim(name: &str, unit: Option<&str>) -> bool {
    match PROTECTED.iter().find(|(organ, _)| *organ == name) {
        Some((_, units)) => unit.is_some_and(|u| units.contains(&u)),
        None => true,
    }
}

fn unit_of_pid(pid: i32) -> Option<String> {
    unit_from_cgroup(&std::fs::read_to_string(format!("/proc/{pid}/cgroup")).ok()?)
}

/// True when this brainstem is the appliance's, run by systemd as
/// being-brainstem.service: only then are organ names checked.
pub fn enforcing() -> bool {
    unit_of_pid(std::process::id() as i32).as_deref() == Some(BRAINSTEM_UNIT)
}

/// The process on the other end of a connection.
#[cfg(target_os = "linux")]
fn peer_pid(stream: &UnixStream) -> Option<i32> {
    use std::os::fd::AsRawFd;
    // SAFETY: an all-zero ucred is a valid value.
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: getsockopt writes at most `len` bytes into the ucred on the
    // stack, and the stream owns the descriptor for the whole call.
    let status = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(),
            &mut len,
        )
    };
    (status == 0 && cred.pid > 0).then_some(cred.pid)
}

#[cfg(not(target_os = "linux"))]
fn peer_pid(_stream: &UnixStream) -> Option<i32> {
    None
}

/// The unit the peer runs in, if the kernel and /proc can say.
pub fn unit_of(stream: &UnixStream) -> Option<String> {
    unit_of_pid(peer_pid(stream)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_unit_is_read_from_the_cgroup_path() {
        assert_eq!(unit_from_cgroup("0::/system.slice/being-mind.service\n").as_deref(), Some("being-mind.service"));
        // A process in a sub-cgroup of its unit still belongs to it.
        assert_eq!(
            unit_from_cgroup("0::/system.slice/being-hands.service/shell\n").as_deref(),
            Some("being-hands.service")
        );
        assert_eq!(
            unit_from_cgroup("12:pids:/x\n1:name=systemd:/system.slice/being-face.service\n").as_deref(),
            Some("being-face.service")
        );
        assert_eq!(unit_from_cgroup("0::/user.slice/user-0.slice/session-2.scope\n"), None);
        assert_eq!(unit_from_cgroup(""), None);
    }

    #[test]
    fn protected_names_need_their_own_unit() {
        assert!(may_claim("mind", Some("being-mind.service")));
        assert!(!may_claim("mind", Some("being-eyes.service")), "the camera sandbox cannot speak as the mind");
        assert!(!may_claim("face", None), "an unknown peer cannot be the face");
        assert!(may_claim("face", Some("being-local-screen.service")));
        assert!(may_claim("voice", Some("being-lab-body.service")));
        assert!(may_claim("keyboard", Some("being-local-screen.service")), "the typing chat");
        assert!(!may_claim("keyboard", Some("being-eyes.service")), "the camera sandbox cannot type as the owner");
        assert!(!may_claim("keyboard", None));
        assert!(may_claim("soak", None), "lab tools keep their own names");
        assert!(may_claim("hardware-timing-12", Some("being-lab-observe.service")));
    }

    #[test]
    fn only_the_mind_unit_can_say_the_mind_is_dreaming() {
        // bodycare holds the package at the dream target on `mind.dreaming`.
        // The topic is the mind's alone (allowed_from), and on the appliance
        // only being-mind.service may call itself the mind.
        for hardware_body in [true, false] {
            assert!(crate::allowed_from("mind.dreaming", "mind", hardware_body));
            for organ in ["bodycare", "eyes", "hands", "voice", "face", "keyboard", "soak"] {
                assert!(!crate::allowed_from("mind.dreaming", organ, hardware_body), "{organ} cannot report a dream");
            }
        }
        assert!(may_claim("mind", Some("being-mind.service")));
        for unit in ["being-bench.service", "being-bodycare.service", "being-hands.service", "being-lab-observe.service"] {
            assert!(!may_claim("mind", Some(unit)), "{unit} cannot speak as the mind");
        }
        assert!(!may_claim("mind", None));
    }

    #[test]
    fn a_development_machine_is_not_the_appliance() {
        assert!(!enforcing());
    }
}
