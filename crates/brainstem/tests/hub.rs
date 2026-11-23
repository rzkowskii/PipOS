//! The real brainstem binary, driven over a real socket.

use bus::{Client, Event};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

struct Stem {
    child: Child,
    dir: PathBuf,
    path: PathBuf,
}

impl Stem {
    fn start(tag: &str, require: &str) -> Stem {
        // Short path: Unix socket paths are limited to about 100 bytes.
        let dir = PathBuf::from(format!("/tmp/bt-{}-{tag}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("bus.sock");
        let child = Command::new(env!("CARGO_BIN_EXE_brainstem"))
            .arg("--bus")
            .arg(&path)
            .arg("--require")
            .arg(require)
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while std::os::unix::net::UnixStream::connect(&path).is_err() {
            assert!(Instant::now() < deadline, "brainstem did not start");
            std::thread::sleep(Duration::from_millis(20));
        }
        Stem { child, dir, path }
    }

    fn join(&self, name: &str, subs: &[&str]) -> Client {
        Client::connect(&self.path, name, subs).unwrap()
    }
}

impl Drop for Stem {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Wait for the first event that satisfies `want`, skipping the rest.
fn wait_for(client: &Client, what: &str, want: impl Fn(&Event) -> bool) -> Event {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        match client.recv_timeout(Duration::from_millis(50)) {
            Ok(Some(ev)) if want(&ev) => return ev,
            Ok(_) => {}
            Err(_) => panic!("bus closed while waiting for {what}"),
        }
    }
    panic!("timed out waiting for {what}");
}

fn state_is(name: &'static str) -> impl Fn(&Event) -> bool {
    move |ev| ev.topic == "state.changed" && ev.str("state") == Some(name)
}

/// True if nothing matching arrives within a short while.
fn quiet(client: &Client, want: impl Fn(&Event) -> bool) -> bool {
    let deadline = Instant::now() + Duration::from_millis(400);
    while Instant::now() < deadline {
        if let Ok(Some(ev)) = client.recv_timeout(Duration::from_millis(50)) {
            if want(&ev) {
                return false;
            }
        }
    }
    true
}

#[test]
fn routes_by_subscription_and_never_echoes() {
    let stem = Stem::start("route", "mind");
    let ears = stem.join("listener", &["hear.*"]);
    let mut voice = stem.join("voice", &["hear.*", "say.*"]);
    voice.send(&Event::new("hear.final").with("text", "hello")).unwrap();
    voice.send(&Event::new("mind.emotion").with("name", "joy")).unwrap();
    let got = wait_for(&ears, "hear.final", |e| e.topic == "hear.final");
    assert_eq!(got.str("text"), Some("hello"));
    assert!(quiet(&ears, |e| !e.topic.starts_with("hear.")), "got a topic it did not ask for");
    assert!(quiet(&voice, |e| e.topic == "hear.final"), "sender got its own message back");
}

#[test]
fn a_newcomer_is_told_the_current_state() {
    let stem = Stem::start("snap", "mind");
    let late = stem.join("late", &["state.changed", "body.senses"]);
    let snap = wait_for(&late, "state snapshot", |e| e.topic == "state.changed");
    assert_eq!(snap.str("state"), Some("waking"));
    let senses = wait_for(&late, "senses snapshot", |e| e.topic == "body.senses");
    assert!(senses.str("line").unwrap().contains("battery"));
}

#[test]
fn boots_then_runs_a_whole_turn() {
    let stem = Stem::start("turn", "mind,voice");
    let watcher = stem.join("watcher", &["state.changed", "face.*", "stem.beat"]);
    let mut mind = stem.join("mind", &["hear.*", "mind.cancel"]);
    let mut voice = stem.join("voice", &["say.*", "voice.mute"]);

    mind.send(&Event::new("mind.ready").with("cortex", true)).unwrap();
    wait_for(&watcher, "idle", state_is("idle"));
    let beat = wait_for(&watcher, "heartbeat", |e| e.topic == "stem.beat");
    assert_eq!(beat.str("state"), Some("idle"));

    voice.send(&Event::new("hear.wake")).unwrap();
    wait_for(&watcher, "attending", state_is("attending"));
    voice.send(&Event::new("hear.speech_start")).unwrap();
    wait_for(&watcher, "listening", state_is("listening"));
    voice.send(&Event::new("hear.final").with("text", "how are you")).unwrap();
    wait_for(&watcher, "thinking", state_is("thinking"));
    let heard = wait_for(&mind, "hear.final at the mind", |e| e.topic == "hear.final");
    assert_eq!(heard.str("text"), Some("how are you"));

    mind.send(&Event::new("mind.emotion").with("name", "joy").with("hold", 3.0)).unwrap();
    let face = wait_for(&watcher, "face.express", |e| e.topic == "face.express");
    assert_eq!(face.str("name"), Some("joy"));

    voice.send(&Event::new("say.started")).unwrap();
    wait_for(&watcher, "speaking", state_is("speaking"));
    voice.send(&Event::new("say.done").with("final", true)).unwrap();
    wait_for(&watcher, "attending again", state_is("attending"));

    // The mute key reaches the voice as an order to close the mic.
    mind.send(&Event::new("key.mute")).unwrap();
    let mute = wait_for(&voice, "voice.mute", |e| e.topic == "voice.mute");
    assert_eq!(mute.bool("on"), Some(true));
}

#[test]
fn only_current_mind_requests_and_voice_receipts_can_announce() {
    let stem = Stem::start("announce", "mind,voice");
    let mut mind = stem.join("mind", &["state.changed", "stem.announce_ready", "say.done"]);
    let mut voice = stem.join("voice", &[]);
    let mut rogue = stem.join("rogue", &[]);
    mind.send(&Event::new("mind.ready")).unwrap();
    wait_for(&mind, "idle", state_is("idle"));

    let request = Event::new("mind.announce").with("announcement", "due-1");
    rogue.send(&request.clone().with("lid_seq", 0)).unwrap();
    rogue.send(&Event::new("stem.announce_ready").with("announcement", "due-1")).unwrap();
    mind.send(&request.clone().with("lid_seq", 1)).unwrap();
    assert!(quiet(&mind, |e| e.topic == "stem.announce_ready" || state_is("thinking")(e)));

    mind.send(&request.with("lid_seq", 0)).unwrap();
    wait_for(&mind, "the announcement to enter thinking", state_is("thinking"));
    let allowed = wait_for(&mind, "the announcement acknowledgement", |e| e.topic == "stem.announce_ready");
    assert_eq!(allowed.str("announcement"), Some("due-1"));

    voice.send(&Event::new("say.started")).unwrap();
    wait_for(&mind, "the announcement to start speaking", state_is("speaking"));
    let receipt = Event::new("say.done").with("final", true).with("announcement", "due-1");
    rogue.send(&receipt).unwrap();
    assert!(quiet(&mind, |e| e.topic == "say.done" || state_is("attending")(e)));
    voice.send(&receipt).unwrap();
    let finished = wait_for(&mind, "the voice receipt", |e| e.topic == "say.done");
    assert_eq!(finished.str("announcement"), Some("due-1"));
    wait_for(&mind, "attending again", state_is("attending"));
}

#[test]
fn a_dream_receipt_marks_only_the_next_live_open_for_that_closure() {
    let stem = Stem::start("dream", "mind");
    let watcher = stem.join("watcher", &["body.lid"]);
    let mut mind = stem.join("mind", &["state.changed"]);
    let mut other = stem.join("other", &[]);
    mind.send(&Event::new("mind.ready")).unwrap();
    wait_for(&mind, "idle", state_is("idle"));
    wait_for(&watcher, "watcher lid snapshot", |e| e.bool("snapshot") == Some(true));

    let lid = |client: &mut Client, closed| {
        client.send(&Event::new("sim.body").with("lid_closed", closed)).unwrap();
    };
    let wait_lid = |closed| wait_for(&watcher, "lid reading", |e| e.topic == "body.lid" && e.bool("closed") == closed);
    let receipt = |seq| Event::new("mind.dream_committed").with("lid_seq", seq);

    lid(&mut other, Some(true));
    let closed = wait_lid(Some(true)).data["seq"].as_u64().unwrap();
    other.send(&receipt(closed)).unwrap();
    lid(&mut other, Some(false));
    assert_eq!(wait_lid(Some(false)).bool("dreamed"), None);

    lid(&mut other, Some(true));
    let closed = wait_lid(Some(true)).data["seq"].as_u64().unwrap();
    mind.send(&receipt(closed - 1)).unwrap();
    lid(&mut mind, Some(false));
    assert_eq!(wait_lid(Some(false)).bool("dreamed"), None);

    lid(&mut other, Some(true));
    let closed = wait_lid(Some(true)).data["seq"].as_u64().unwrap();
    mind.send(&receipt(closed)).unwrap();
    lid(&mut mind, None);
    assert_eq!(wait_lid(None).bool("dreamed"), None);
    lid(&mut mind, Some(false));
    assert_eq!(wait_lid(Some(false)).bool("dreamed"), None);

    lid(&mut other, Some(true));
    let closed = wait_lid(Some(true)).data["seq"].as_u64().unwrap();
    mind.send(&receipt(closed)).unwrap();
    mind.send(&receipt(closed)).unwrap();
    lid(&mut mind, Some(false));
    assert_eq!(wait_lid(Some(false)).bool("dreamed"), Some(true));
    let late = stem.join("late", &["body.lid"]);
    let snap = wait_for(&late, "retained open", |e| e.topic == "body.lid" && e.bool("snapshot") == Some(true));
    assert_eq!(snap.bool("dreamed"), None);

    lid(&mut mind, Some(true));
    wait_lid(Some(true));
    lid(&mut mind, Some(false));
    assert_eq!(wait_lid(Some(false)).bool("dreamed"), None);
}

#[test]
fn losing_an_organ_shows_and_its_return_clears_it() {
    let stem = Stem::start("lost", "mind,voice");
    let watcher = stem.join("watcher", &["state.changed", "organ.*"]);
    let mut mind = stem.join("mind", &[]);
    let voice = stem.join("voice", &[]);
    mind.send(&Event::new("mind.ready")).unwrap();
    wait_for(&watcher, "idle", state_is("idle"));

    drop(voice);
    wait_for(&watcher, "voice down", |e| e.topic == "organ.down" && e.str("name") == Some("voice"));
    wait_for(&watcher, "unwell", |e| e.topic == "state.changed" && e.bool("unwell") == Some(true));

    let _voice = stem.join("voice", &[]);
    wait_for(&watcher, "voice back up", |e| e.topic == "organ.up" && e.str("name") == Some("voice"));
    wait_for(&watcher, "well again", |e| e.topic == "state.changed" && e.bool("unwell") == Some(false));
}

#[test]
fn a_restarted_organ_replaces_its_old_self_without_alarm() {
    let stem = Stem::start("swap", "mind,voice");
    let watcher = stem.join("watcher", &["state.changed", "organ.*"]);
    let mut mind = stem.join("mind", &[]);
    let _old = stem.join("voice", &[]);
    mind.send(&Event::new("mind.ready")).unwrap();
    wait_for(&watcher, "idle", state_is("idle"));

    // The new voice connects before the old connection has gone away.
    let _new = stem.join("voice", &[]);
    assert!(quiet(&watcher, |e| e.topic == "organ.down" || e.bool("unwell") == Some(true)));
}

#[test]
fn organs_cannot_forge_brainstem_topics() {
    let stem = Stem::start("forge", "mind");
    let watcher = stem.join("watcher", &["*"]);
    wait_for(&watcher, "watcher subscription", |e| e.topic == "body.lid");
    let mut rogue = stem.join("rogue", &[]);
    rogue.send(&Event::new("state.changed").with("state", "asleep")).unwrap();
    rogue.send(&Event::new("face.express").with("name", "joy")).unwrap();
    rogue.send(&Event::new("hear.wake")).unwrap();
    wait_for(&watcher, "the honest event", |e| e.topic == "hear.wake");
    assert!(quiet(&watcher, |e| e.topic == "face.express" || e.str("state") == Some("asleep")));
}

#[test]
fn camera_gaze_requires_opt_in_and_current_open_lid() {
    let stem = Stem::start("camera", "");
    let watcher = stem.join("watcher", &["*"]);
    let metadata = stem.join("metadata", &["body.lid", "see.*", "camera.request"]);
    wait_for(&metadata, "metadata subscription", |e| e.topic == "body.lid");
    let mut eyes = stem.join("eyes", &["body.lid", "camera.policy"]);
    let mut other = stem.join("other", &[]);
    let grant = |ev: &Event| ev.topic == "camera.policy" && ev.bool("allowed") == Some(true);
    let revoke = |ev: &Event| ev.topic == "camera.policy" && ev.bool("allowed") == Some(false);
    let cleared = |ev: &Event| ev.topic == "face.gaze" && ev.bool("present") == Some(false);
    let camera_signal = |ev: &Event| matches!(ev.topic.as_str(), "face.gaze" | "body.presence");
    let check_presence = |present: bool, lid_seq: u64| {
        let event = wait_for(&watcher, "presence from the brainstem", |e| e.topic == "body.presence");
        assert_eq!(event.bool("present"), Some(present));
        assert_eq!(event.data["lid_seq"].as_u64(), Some(lid_seq));
        assert_eq!(event.data.len(), 2, "presence carries only the reading and lid epoch");
    };
    let seq = |ev: &Event| ev.data["seq"].as_u64().unwrap();
    let ask = |enabled| Event::new("camera.request").with("enabled", enabled);
    let detection = |lid_seq: u64, camera_seq: u64| {
        Event::new("see.presence").with("present", true).with("x", 0.4).with("y", -0.1)
            .with("lid_seq", lid_seq).with("camera_seq", camera_seq)
    };

    let initial = wait_for(&eyes, "initial camera policy", revoke);
    assert_eq!(seq(&initial), 0);
    wait_for(&watcher, "idle", state_is("idle"));
    other.send(&ask(true)).unwrap();
    other.send(&Event::new("camera.policy").with("allowed", true).with("seq", 1)).unwrap();
    other.send(&Event::new("body.presence").with("present", true).with("lid_seq", 0)).unwrap();
    eyes.send(&detection(0, 0)).unwrap();
    assert!(quiet(&watcher, |e| grant(e) || camera_signal(e)));

    eyes.send(&ask(true)).unwrap();
    let mut camera_seq = seq(&wait_for(&eyes, "camera opt-in", grant));
    other.send(&detection(0, camera_seq)).unwrap();
    eyes.send(&detection(0, camera_seq + 1)).unwrap();
    eyes.send(&detection(1, camera_seq)).unwrap();
    eyes.send(&Event::new("see.presence").with("present", true)
        .with("lid_seq", 0).with("camera_seq", camera_seq)).unwrap();
    eyes.send(&Event::new("see.attention")).unwrap();
    assert!(quiet(&watcher, |e| camera_signal(e) || state_is("attending")(e)));

    eyes.send(&detection(0, camera_seq)).unwrap();
    let gaze = wait_for(&watcher, "camera-driven gaze", |e| e.topic == "face.gaze");
    assert_eq!((gaze.f64("x"), gaze.f64("y")), (Some(0.4), Some(-0.1)));
    check_presence(true, 0);
    eyes.send(&Event::new("see.presence").with("present", false)
        .with("lid_seq", 1).with("camera_seq", camera_seq)).unwrap();
    assert!(quiet(&watcher, camera_signal));
    assert!(quiet(&watcher, state_is("attending")));
    assert!(quiet(&metadata, |e| e.topic != "body.lid"), "raw camera data must stay within the hub");
    eyes.send(&Event::new("see.presence").with("present", false)
        .with("lid_seq", 0).with("camera_seq", camera_seq)).unwrap();
    wait_for(&watcher, "clear on absence", cleared);
    check_presence(false, 0);

    eyes.send(&detection(0, camera_seq)).unwrap();
    wait_for(&watcher, "gaze before lid loss", |e| e.topic == "face.gaze" && e.f64("x").is_some());
    check_presence(true, 0);
    other.send(&Event::new("sim.body").with("lid_closed", serde_json::Value::Null)).unwrap();
    let revoked_seq = seq(&wait_for(&eyes, "revoke on unknown lid", revoke));
    assert!(revoked_seq > camera_seq);
    wait_for(&watcher, "clear on loss of lid reading", cleared);
    check_presence(false, 1);
    eyes.send(&detection(1, revoked_seq)).unwrap();
    assert!(quiet(&watcher, camera_signal));
    other.send(&Event::new("sim.body").with("lid_closed", false)).unwrap();
    camera_seq = seq(&wait_for(&eyes, "fresh grant after waking", grant));
    assert!(camera_seq > revoked_seq);
    eyes.send(&detection(0, camera_seq)).unwrap();
    eyes.send(&detection(2, revoked_seq)).unwrap();
    assert!(quiet(&watcher, camera_signal));

    eyes.send(&detection(2, camera_seq)).unwrap();
    wait_for(&watcher, "gaze before disconnect", |e| e.topic == "face.gaze");
    check_presence(true, 2);
    drop(eyes);
    wait_for(&watcher, "clear on disconnect", cleared);
    check_presence(false, 2);
    let mut eyes = stem.join("eyes", &["camera.policy"]);
    let denied = wait_for(&eyes, "new eyes must ask again", revoke);
    assert!(seq(&denied) > camera_seq);
    eyes.send(&ask(true)).unwrap();
    camera_seq = seq(&wait_for(&eyes, "new opt-in", grant));
    eyes.send(&detection(2, camera_seq)).unwrap();
    wait_for(&watcher, "gaze before opt-out", |e| e.topic == "face.gaze");
    check_presence(true, 2);
    eyes.send(&ask(false)).unwrap();
    wait_for(&eyes, "owner opt-out", revoke);
    wait_for(&watcher, "clear on opt-out", cleared);
    check_presence(false, 2);
}

#[test]
fn a_second_brainstem_refuses_to_start() {
    let stem = Stem::start("twice", "mind");
    let status = Command::new(env!("CARGO_BIN_EXE_brainstem"))
        .arg("--bus")
        .arg(&stem.path)
        .stderr(Stdio::null())
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(2));
    // The first one is still serving.
    let c = stem.join("check", &["state.changed"]);
    wait_for(&c, "snapshot from the survivor", |e| e.topic == "state.changed");
}

/// Start a brainstem with a life record in `mind`, as boot number `boot`.
fn live(mind: &std::path::Path, sock: &std::path::Path, boot: &str, body: &str) -> Child {
    let child = Command::new(env!("CARGO_BIN_EXE_brainstem"))
        .arg("--bus")
        .arg(sock)
        .arg("--mind-dir")
        .arg(mind)
        .env("BEING_BOOT_ID", boot)
        .env("BEING_BODY", body)
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while std::os::unix::net::UnixStream::connect(sock).is_err() {
        assert!(Instant::now() < deadline, "brainstem did not start");
        std::thread::sleep(Duration::from_millis(20));
    }
    child
}

fn woke(sock: &std::path::Path) -> Event {
    let client = Client::connect(sock, "observer", &["life.*", "body.senses"]).unwrap();
    wait_for(&client, "life.woke", |e| e.topic == "life.woke")
}

fn terminate(mut child: Child) -> std::process::ExitStatus {
    // SAFETY: plain signal to our own child.
    unsafe { libc_kill(child.id() as i32, 15) };
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("brainstem did not stop after SIGTERM");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
}

#[test]
fn it_is_born_once_and_knows_how_each_wake_began() {
    let dir = PathBuf::from(format!("/tmp/bt-{}-life", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let mind = dir.join("mind");
    std::fs::create_dir_all(&mind).unwrap();
    let sock = dir.join("bus.sock");

    // Born.
    let stem = live(&mind, &sock, "boot-1", "lab machine");
    let observer = Client::connect(&sock, "observer", &["life.woke"]).unwrap();
    let first = wait_for(&observer, "life.woke", |e| e.topic == "life.woke");
    assert_eq!(first.bool("first"), Some(true));
    assert_eq!(first.f64("wakes"), Some(1.0));
    let id = first.str("id").unwrap().to_string();
    let born = first.f64("born").unwrap();

    // With the observer still connected but quiet, the next tick must let it stop.
    assert!(terminate(stem).success());
    assert!(!sock.exists(), "it should tidy its socket away when it sleeps");
    drop(observer);

    // A normal wake: same being, one more wake, no alarm.
    let mut stem = live(&mind, &sock, "boot-2", "lab machine");
    let second = woke(&sock);
    assert_eq!(second.str("id"), Some(id.as_str()));
    assert_eq!(second.f64("born"), Some(born));
    assert_eq!(second.f64("wakes"), Some(2.0));
    assert_eq!(second.bool("lost_power"), Some(false));
    assert_eq!(second.bool("first"), Some(false));

    // The plug is pulled.
    stem.kill().unwrap();
    stem.wait().unwrap();
    let _ = std::fs::remove_file(&sock);
    let stem = live(&mind, &sock, "boot-3", "lab machine");
    let third = woke(&sock);
    assert_eq!(third.bool("lost_power"), Some(true));
    assert_eq!(third.f64("wakes"), Some(3.0));
    // And the senses line says so.
    let client = Client::connect(&sock, "listener", &["body.senses"]).unwrap();
    let senses = wait_for(&client, "senses with the news", |e| {
        e.topic == "body.senses" && e.str("line").is_some_and(|l| l.contains("lost power"))
    });
    assert!(senses.str("line").unwrap().contains("awake 1 min"));
    drop(client);
    assert!(terminate(stem).success());

    // Its memory wakes in different hardware.
    let stem = live(&mind, &sock, "boot-4", "Apple Inc. / MacBookPro9,2");
    let fourth = woke(&sock);
    assert_eq!(fourth.bool("new_body"), Some(true));
    assert_eq!(fourth.bool("lost_power"), Some(false));
    assert_eq!(fourth.str("id"), Some(id.as_str()));
    assert!(terminate(stem).success());
    let _ = std::fs::remove_dir_all(&dir);
}
