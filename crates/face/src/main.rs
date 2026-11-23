//! The face: two glowing eyes drawn straight to the screen.
//!
//! On the laptop it runs full-screen on KMS/DRM with no window system
//! (SDL_VIDEODRIVER=kmsdrm). On a development Mac it runs in a window.

mod anim;
mod dpms;
mod eye;
mod link;
mod overlay;
mod pace;
mod panel;
mod render;
mod status;
mod text;

use bus::Event;
use sdl2::event::{Event as SdlEvent, WindowEvent};
use sdl2::keyboard::Keycode;
use sdl2::mouse::MouseButton;
use sdl2::video::GLProfile;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const HELP: &str = "\
face: the being's eyes

  face                         connect to the brainstem and show its state
  face --demo                  cycle through expressions and a dreaming preview
  face --standalone            idle on its own, no brainstem needed
  face --fullscreen            cover the screen (always the case on the laptop)
  face --size 1280x800         window size
  face --scale N               draw at 1/N size and stretch (default: 4 on a
                               software renderer, otherwise 1)
  face --bus PATH              bus socket (default: see docs/PROTOCOL.md)
  face --list                  print the expression names
  face --snapshot FILE.png [--expr NAME] [--size WxH]    render one still image
  face --sheet FILE.png [--names a,b,c] [--size WxH]     render a sheet of stills
                               (each tile is WxH if given, else 640x400)

For tests: --hidden shows no window, --exit-after SECS quits by itself, and
--record FILE.png saves what the face looked like at the moment it quit.

For reviewing motion off-screen:
  face --script NAME           play a scripted scene instead of the brainstem:
                               tour (wake, attention, thought, speech, emotions,
                               idle life, a yawn, sleep), drowse (a long quiet
                               spell: heavy lids and a yawn, then company) or
                               birth (the first power-on)
  face --frames DIR [--fps N]  run on a fixed clock with no window and save
                               every frame as DIR/frame-00000.png (default 30
                               fps); pair with --demo or --script, --size and
                               --exit-after, which then counts scene seconds
                               (default: the end of the scene).
                               ffmpeg -framerate 30 -i DIR/frame-%05d.png out.mp4
  face --marks glint,ring,ears,network,ssh   show privacy marks (stills, frames)
  face --seed N                the same idle life every run (blinks, glances and
                               gestures), for reproducible frame dumps

Keys: Esc interrupts it, M mutes it, F1 shows captions and health (off by default and
never logged), F2 opens appliance setup, F9 recovers a face with no lid reading, and Q
closes the window. With the panel dark and the brainstem silent, any key shows the face.
While an app panel has focus, keys go to it (M is a letter then; F10 still mutes).

Full screen, the face shows itself after a few seconds with no lid reading
(waiting calmly for the first minute, then dazed if the brainstem is gone), turns
the panel's connector off while dark (BEING_FACE_DPMS=0 leaves it on), and draws
at 60, 30, 15 or 5 frames a second as what it shows needs. With being.verbose=1 on the kernel command
line it first leaves the boot messages up for 20 s (being.verbose_hold=SECS,
0 to wait for a key); Esc, F2, Enter or Space ends the wait.";

struct Args {
    bus: PathBuf,
    fullscreen: bool,
    size: (u32, u32),
    /// --size was given (a sheet then uses it for each tile).
    size_given: bool,
    demo: bool,
    standalone: bool,
    list: bool,
    snapshot: Option<PathBuf>,
    sheet: Option<PathBuf>,
    expr: String,
    names: Vec<String>,
    hidden: bool,
    scale: Option<i32>,
    exit_after: Option<f64>,
    record: Option<PathBuf>,
    script: Option<String>,
    frames: Option<PathBuf>,
    fps: f64,
    seed: Option<u32>,
    /// Privacy marks forced on, for reviewing stills and frames.
    marks: Option<render::Marks>,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        bus: bus::socket_path(),
        fullscreen: false,
        size: (1280, 800),
        size_given: false,
        demo: false,
        standalone: false,
        list: false,
        snapshot: None,
        sheet: None,
        expr: "neutral".to_string(),
        names: Vec::new(),
        hidden: false,
        scale: None,
        exit_after: None,
        record: None,
        script: None,
        frames: None,
        fps: 30.0,
        seed: None,
        marks: None,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut value = |name: &str| it.next().ok_or(format!("{name} needs a value"));
        match arg.as_str() {
            "--bus" => a.bus = PathBuf::from(value("--bus")?),
            "--fullscreen" => a.fullscreen = true,
            "--demo" => a.demo = true,
            "--standalone" => a.standalone = true,
            "--list" => a.list = true,
            "--hidden" => a.hidden = true,
            "--scale" => {
                let n: i32 = value("--scale")?.parse().map_err(|_| "--scale wants a whole number")?;
                if !(1..=8).contains(&n) {
                    return Err("--scale must be between 1 and 8".to_string());
                }
                a.scale = Some(n);
            }
            "--record" => a.record = Some(PathBuf::from(value("--record")?)),
            "--exit-after" => {
                let secs: f64 = value("--exit-after")?.parse().map_err(|_| "--exit-after wants seconds")?;
                a.exit_after = Some(secs);
            }
            "--script" => {
                let name = value("--script")?;
                if !SCRIPTS.contains(&name.as_str()) {
                    return Err(format!("--script wants one of {}", SCRIPTS.join(", ")));
                }
                a.script = Some(name);
            }
            "--frames" => a.frames = Some(PathBuf::from(value("--frames")?)),
            "--seed" => a.seed = Some(value("--seed")?.parse().map_err(|_| "--seed wants a whole number")?),
            "--marks" => a.marks = Some(parse_marks(&value("--marks")?)?),
            "--fps" => {
                let fps: f64 = value("--fps")?.parse().map_err(|_| "--fps wants a number")?;
                if !(1.0..=120.0).contains(&fps) {
                    return Err("--fps must be between 1 and 120".to_string());
                }
                a.fps = fps;
            }
            "--snapshot" => a.snapshot = Some(PathBuf::from(value("--snapshot")?)),
            "--sheet" => a.sheet = Some(PathBuf::from(value("--sheet")?)),
            "--expr" => a.expr = value("--expr")?,
            "--names" => a.names = value("--names")?.split(',').map(str::to_string).collect(),
            "--size" => {
                let v = value("--size")?;
                let (w, h) = v.split_once('x').ok_or("--size wants WIDTHxHEIGHT")?;
                let w: u32 = w.parse().map_err(|_| "--size wants WIDTHxHEIGHT")?;
                let h: u32 = h.parse().map_err(|_| "--size wants WIDTHxHEIGHT")?;
                if !(64..=8192).contains(&w) || !(64..=8192).contains(&h) {
                    return Err("--size must be between 64 and 8192 each way".to_string());
                }
                a.size = (w, h);
                a.size_given = true;
            }
            "-h" | "--help" => {
                println!("{HELP}");
                std::process::exit(0);
            }
            other => return Err(format!("unknown option {other}\n\n{HELP}")),
        }
    }
    // Saved frames never end on their own; stop a scene at its natural end
    // rather than filling the disk.
    if a.frames.is_some() && a.exit_after.is_none() {
        a.exit_after = Some(match a.script.as_deref() {
            Some("tour") => 38.0,
            Some("drowse") => 16.0,
            Some("birth") => 9.0,
            _ if a.demo => 2.5 + 2.4 * eye::NAMES.len() as f64,
            _ => return Err("--frames needs --script, --demo or --exit-after".to_string()),
        });
    }
    Ok(a)
}

fn main() {
    if let Err(e) = run() {
        eprintln!("face: {e}");
        std::process::exit(1);
    }
}

/// Open a window with a GL context: desktop OpenGL 3.3 first, then GLES 3.0.
fn make_window(
    video: &sdl2::VideoSubsystem,
    args: &Args,
    hidden: bool,
) -> Result<(sdl2::video::Window, sdl2::video::GLContext), String> {
    let mut last_err = String::new();
    for (profile, major, minor) in [(GLProfile::Core, 3, 3), (GLProfile::GLES, 3, 0)] {
        let attr = video.gl_attr();
        attr.set_context_profile(profile);
        attr.set_context_version(major, minor);
        // Flags stick between attempts, and "forward compatible" is only valid
        // for desktop OpenGL: left set, it makes the GLES attempt fail.
        if profile == GLProfile::Core {
            attr.set_context_flags().forward_compatible().set();
        } else {
            attr.set_context_flags().set();
        }
        let mut builder = video.window("Being", args.size.0, args.size.1);
        builder.opengl().allow_highdpi();
        if hidden {
            builder.hidden();
        } else if args.fullscreen {
            builder.fullscreen_desktop();
        } else {
            builder.position_centered().resizable();
        }
        let window = match builder.build() {
            Ok(w) => w,
            Err(e) => {
                last_err = e.to_string();
                continue;
            }
        };
        match window.gl_create_context() {
            Ok(ctx) => return Ok((window, ctx)),
            Err(e) => last_err = e,
        }
    }
    Err(format!("could not get an OpenGL 3.3 or GLES 3.0 context: {last_err}"))
}

fn run() -> Result<(), String> {
    let args = parse_args()?;
    if args.list {
        for name in eye::NAMES {
            println!("{name}");
        }
        return Ok(());
    }
    let offscreen = args.snapshot.is_some() || args.sheet.is_some();
    #[cfg(target_os = "linux")]
    if args.fullscreen && !offscreen && !args.hidden && args.frames.is_none() {
        verbose::hold_for_boot_messages("face");
    }

    let sdl = sdl2::init()?;
    let video = sdl.video()?;
    let (window, _ctx) = make_window(&video, &args, offscreen || args.hidden || args.frames.is_some())?;
    let gl = unsafe { glow::Context::from_loader_function(|s| video.gl_get_proc_address(s) as *const _) };
    let mut renderer = unsafe { render::Renderer::new(&gl)? };
    let mut look = render::Look::default();

    if let Some(marks) = args.marks {
        look.marks = marks;
    }
    if offscreen {
        return stills(&gl, &renderer, &look, &args);
    }

    // A software renderer (a virtual machine, or a GPU with no driver) cannot
    // fill the screen sixty times a second. Draw small and stretch, and take
    // fewer frames, so the rest of the being still gets the CPU.
    let (drawn_by, software) = unsafe { render::describe(&gl) };
    let scale = args.scale.unwrap_or(if software { 4 } else { 1 });
    if scale > 1 {
        look.grid = 0.0; // the LED texture does not survive stretching
    }
    eprintln!(
        "face: drawn by {drawn_by}{}",
        if scale > 1 { format!(", at 1/{scale} size") } else { String::new() }
    );

    // A hidden window is never presented, so there is no vsync to wait on.
    // Saved frames run on their own clock, as fast as they can be written.
    let recording = args.frames.is_some();
    let hidden = args.hidden || recording;
    let vsync = !hidden && video.gl_set_swap_interval(1).is_ok();
    let fast_refresh = |display| vsync && video.current_display_mode(display).is_ok_and(|mode| mode.refresh_rate > 60);
    let mut fast_display = window.display_index().is_ok_and(fast_refresh);
    if args.fullscreen {
        sdl.mouse().show_cursor(false);
    }
    let mut events = sdl.event_pump()?;
    let local_setup = cfg!(target_os = "linux") && std::env::var("BEING_LOCAL_SETUP").as_deref() == Ok("1");
    let appliance = cfg!(target_os = "linux") && args.fullscreen && !hidden;

    let seed = args.seed.unwrap_or_else(|| {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(1)
    });
    let mut anim = anim::Anim::new(seed);
    let solo = args.demo || args.standalone || args.script.is_some();
    // The appliance starts dark until the brainstem reports the lid, but not
    // for long: with no lid reading it shows itself (dazed if the brainstem
    // is gone) rather than leave a black panel.
    let lid_grace = args.fullscreen.then_some(link::LID_GRACE);
    let mut link = if solo { None } else { Some(link::Link::new(args.bus.clone(), lid_grace)) };
    if let Some(link) = &mut link {
        link.side = link::Side::new(appliance.then(|| PathBuf::from(BIRTH_MARKER)));
    }
    if solo {
        anim.set_state("idle", false, false);
        if args.demo || args.script.is_some() {
            anim.set_lid(Some(false), false, false);
        }
    } else if args.fullscreen {
        anim.set_lid(None, false, true);
    }

    // The connector goes off while dark, on SDL's own DRM handle (task 2.2).
    let dpms_wanted = appliance && std::env::var("BEING_FACE_DPMS").as_deref() != Ok("0");
    let drm = dpms_wanted && dpms::sdl_drm_fd(&window).is_some();
    if dpms_wanted && !drm {
        eprintln!("face: SDL gives no KMS/DRM handle; the panel stays powered while dark");
    }
    let mut power = dpms::Power::new(drm);
    let mut connectors = dpms::Connectors::default();
    let watch = if appliance { status::Watch::start(PathBuf::from("/")) } else { status::Watch::none() };
    let mut panel = Some(panel::Panel::new(
        std::env::var_os("BEING_PANEL_DIR").map_or_else(|| PathBuf::from(panel::DIR), PathBuf::from),
    ));
    let mut keys = panel::KeySender::new(std::env::var_os("BEING_HANDS_KEYS").map_or_else(|| PathBuf::from(panel::KEYS), PathBuf::from));
    let mut panel_tex: Option<render::Texture> = None;
    // The picture changed while hidden, so the texture is out of date.
    let mut panel_behind = false;
    let mut panel_amount = 0f32;
    let mut panel_focus = false;
    let mut overlay_tex: Option<render::Texture> = None;
    let mut overlay_drawn: Option<Instant> = None;
    let mut note_tex: Option<render::Texture> = None;
    let mut stats = pace::Stats::new(Instant::now());
    let mut frame_ms = 0f64;
    let mut rate = 60u32;

    let mut demo_next = 2.5f64;
    let mut demo_index = 0usize;
    let started = Instant::now();
    let mut last = started;
    let mut pose = eye::preset("asleep").unwrap();
    let mut blank_presented = false;
    let mut deadline: Option<Instant> = None;
    let mut frame_no = 0u64;
    let mut clock = 0f64;
    let mut skip = 0f32;

    'run: loop {
        let frame_start = Instant::now();
        let scene_time = if recording { clock } else { started.elapsed().as_secs_f64() };
        if args.exit_after.is_some_and(|secs| scene_time >= secs) {
            break 'run;
        }
        for event in events.poll_iter() {
            match event {
                SdlEvent::Quit { .. } => break 'run,
                SdlEvent::Window { win_event: WindowEvent::DisplayChanged(display), .. } => {
                    fast_display = fast_refresh(display);
                }
                SdlEvent::KeyDown { keycode: Some(key), keymod, repeat, .. } => {
                    // Typing into an open, focused app panel goes to hands.
                    // Esc, F1, F2, F9 and F10 keep their meaning; M is a letter.
                    let reserved = [Keycode::Escape, Keycode::F1, Keycode::F2, Keycode::F9, Keycode::F10].contains(&key);
                    if panel_focus && !reserved {
                        if let Some(bytes) = panel::key_bytes(key, keymod) {
                            keys.send(&bytes);
                        }
                        continue;
                    }
                    if repeat {
                        continue;
                    }
                    let dark_and_alone = anim.panel_blank() && link.as_ref().is_some_and(|l| l.quiet());
                    if key == Keycode::Escape {
                        if let Some(link) = &mut link {
                            link.send(Event::new("key.interrupt"));
                        }
                    } else if key == Keycode::F1 {
                        if let Some(link) = &mut link {
                            link.side.captions.toggle();
                        }
                        overlay_drawn = None;
                    } else if key == Keycode::M || key == Keycode::F10 {
                        if let Some(link) = &mut link {
                            link.send(Event::new("key.mute"));
                        }
                    } else if key == Keycode::F9 {
                        if let Some(link) = &mut link {
                            link.send(Event::new("key.panel_recover"));
                        }
                    } else if key == Keycode::F2 && local_setup {
                        let _ = std::process::Command::new("systemctl")
                            .args(["--no-block", "start", "being-local-screen.service"])
                            .status();
                    } else if key == Keycode::Q && !args.fullscreen {
                        break 'run;
                    }
                    if dark_and_alone {
                        if let Some(link) = &mut link {
                            link.unblank_locally(&mut anim);
                        }
                    }
                }
                // SDL may synthesize a mouse event from the same touch, or
                // a touch from a mouse click. Ignore the synthetic copies.
                SdlEvent::MouseButtonDown { mouse_btn: MouseButton::Left, which, .. } if which != u32::MAX => {
                    if let Some(link) = &mut link {
                        link.send(Event::new("key.touch"));
                    }
                }
                SdlEvent::FingerDown { touch_id, .. } if touch_id != -1 => {
                    if let Some(link) = &mut link {
                        link.send(Event::new("key.touch"));
                    }
                }
                _ => {}
            }
        }

        let now = Instant::now();
        let mut dt = now.duration_since(last).as_secs_f32();
        last = now;
        let previous = clock;
        if recording {
            dt = if frame_no == 0 { 0.0 } else { (1.0 / args.fps) as f32 };
            clock += f64::from(dt);
        } else {
            clock = now.duration_since(started).as_secs_f64();
        }
        dt += std::mem::take(&mut skip);
        pose = anim.frame(dt, |anim| {
            if let Some(link) = &mut link {
                link.poll(anim);
            }
            let t = clock;
            if let Some(name) = &args.script {
                skip = scene(anim, name, previous, t);
            }
            if args.demo && t >= demo_next {
                let name = eye::NAMES[demo_index % eye::NAMES.len()];
                demo_index += 1;
                demo_next = t + 2.4;
                match name {
                    "asleep" => anim.set_state("asleep", false, false),
                    "dreaming" => anim.preview_dreaming(),
                    "muted" => anim.set_state("idle", true, false),
                    "dazed" => anim.set_state("idle", false, true),
                    _ => {
                        anim.set_state("idle", false, false);
                        anim.express(name, 2.4);
                    }
                }
            }
        });
        let panel_blank = anim.panel_blank();

        // The workshop panel: read what is new, and let its focus steer the
        // eyes. Nothing is read while the screen is dark, and nothing is
        // uploaded while the face covers the panel in its sleep (the app's
        // own state does not change).
        let mut shown = None;
        let covered = anim.asleep();
        if let (Some(panel), false) = (&mut panel, panel_blank) {
            let update = panel.poll(now);
            if let Some(header) = panel.shown(now).filter(|_| !covered) {
                shown = Some(header.clone());
                let (w, h) = (panel.width as i32, panel.height as i32);
                // Rows that changed while the panel was hidden never reached
                // the texture: upload the whole picture once it shows again.
                let behind = std::mem::take(&mut panel_behind);
                unsafe {
                    match (&panel_tex, update) {
                        (Some(tex), panel::Update::Rows(first, count)) if tex.w == w && tex.h == h && !behind => {
                            renderer.upload_rows(&gl, tex, first as i32, count as i32, &panel.pixels);
                        }
                        (Some(tex), panel::Update::Nothing) if tex.w == w && tex.h == h && !behind => {}
                        _ => {
                            if let Some(old) = panel_tex.take() {
                                renderer.delete(&gl, old);
                            }
                            panel_tex = Some(renderer.texture(&gl, w, h, &panel.pixels)?);
                        }
                    }
                }
            } else if update != panel::Update::Nothing {
                panel_behind = true;
            }
            let flags = panel.flags(now);
            anim.set_typing_hidden(shown.is_none() && flags & panel::flag::PIP_TYPING != 0);
        } else if let (Some(panel), true) = (&mut panel, panel_blank) {
            // Nothing of an app's screen stays in the face while it is dark;
            // the whole frame is read again when the panel lights.
            if !panel.pixels.is_empty() {
                panel.forget_picture();
            }
        }
        let target = if shown.is_some() { 1.0 } else { 0.0 };
        let step = dt.min(0.1) / panel::TRANSITION;
        panel_amount = if target > panel_amount { (panel_amount + step).min(1.0) } else { (panel_amount - step).max(0.0) };
        panel_focus = shown.as_ref().is_some_and(|h| h.has(panel::flag::FOCUS)) && panel_amount > 0.5;
        anim.set_focus(shown.as_ref().and_then(|h| {
            let gaze = |point: Option<(u32, u32)>| point.map(|(x, y)| panel::gaze_at(x, y, h.width, h.height));
            if h.has(panel::flag::PIP_PAUSED) {
                Some(anim::Focus { kind: anim::FocusKind::Paused, at: [0.0, 0.1] })
            } else if h.has(panel::flag::PIP_TYPING) {
                gaze(h.cursor_point()).map(|at| anim::Focus { kind: anim::FocusKind::Typing, at })
            } else if h.has(panel::flag::JOB_BUSY) {
                gaze(h.newest_point()).map(|at| anim::Focus { kind: anim::FocusKind::Watching, at })
            } else {
                None
            }
        }));
        // Once the panel has faded out, its texture goes too.
        if shown.is_none() && panel_amount == 0.0 {
            if let Some(old) = panel_tex.take() {
                unsafe { renderer.delete(&gl, old) };
            }
        }

        // Privacy on the face (task 7.5).
        let privacy = watch.get();
        let camera = link.as_ref().is_some_and(|l| l.side.camera);
        let on = |b: bool| if b { 1.0 } else { 0.0 };
        look.marks = render::Marks {
            glint: on(camera && !anim.asleep()),
            ring: on(privacy.guest),
            ears: on(anim.muted()),
            network: on(privacy.network),
            ssh: on(privacy.ssh),
        };
        if let Some(marks) = args.marks {
            look.marks = marks;
        }

        // The connector is turned on before the first lit frame and off once
        // the black frame has rested on scanout.
        match power.step(now, panel_blank, panel_blank && blank_presented) {
            dpms::Step::Nothing => {}
            step => {
                let off = step == dpms::Step::TurnOff;
                let value = if off { dpms::DPMS_OFF } else { dpms::DPMS_ON };
                let result = dpms::sdl_drm_fd(&window)
                    .ok_or_else(|| "no DRM handle".to_string())
                    .and_then(|fd| connectors.set(fd, value));
                match result {
                    Ok(_) => eprintln!("face: panel connector {}", if off { "off while dark" } else { "on" }),
                    Err(e) => {
                        let which = if off { "off" } else { "on" };
                        eprintln!("face: could not turn the panel connector {which} ({e}); leaving it to the backlight");
                        power.restore();
                        power.disable();
                    }
                }
            }
        }

        let (w, h) = if hidden {
            (args.size.0 as i32, args.size.1 as i32)
        } else {
            let (w, h) = window.drawable_size();
            (w as i32, h as i32)
        };
        if !panel_blank {
            // Captions and health, behind F1. Redrawn at most twice a second.
            let open = link.as_ref().is_some_and(|l| l.side.captions.on);
            if open {
                let changed = link.as_mut().is_some_and(|l| l.side.captions.take_changed());
                let stale = overlay_drawn.is_none_or(|at| now.saturating_duration_since(at) >= Duration::from_millis(500));
                if changed || stale || overlay_tex.is_none() {
                    overlay_drawn = Some(now);
                    let health = overlay::Health {
                        renderer: drawn_by.clone(),
                        software,
                        scale,
                        hz: rate,
                        frame_ms,
                        beat_age: link.as_ref().and_then(|l| l.beat_age()),
                        camera,
                        privacy,
                        network_mode: if appliance { status::network_mode(Path::new("/")) } else { None },
                        dpms_off: power.is_off(),
                    };
                    if let (Some(font), Some(link)) = (text::font(), &link) {
                        let zoom = (h / 800).max(1) as usize;
                        let columns = ((w as usize / zoom).saturating_sub(24) / font.w).clamp(40, 120);
                        let lines = link.side.captions.lines(&health, columns);
                        let pic = font.render(&lines, columns, text::Ink { rgb: CAPTION_INK, backing: 200 });
                        unsafe {
                            if let Some(old) = overlay_tex.take() {
                                renderer.delete(&gl, old);
                            }
                            overlay_tex = Some(renderer.texture(&gl, pic.w as i32, pic.h as i32, &pic.rgba)?);
                        }
                    }
                }
            } else if let Some(old) = overlay_tex.take() {
                unsafe { renderer.delete(&gl, old) };
            }
            // A software renderer says so on screen for its first half
            // minute, too dim for anyone's eye detector to count.
            if software && scene_time < SOFTWARE_NOTE.as_secs_f64() {
                if note_tex.is_none() {
                    if let Some(font) = text::font() {
                        let line = format!("drawn in software ({drawn_by}): slower, at 1/{scale} size");
                        let columns = line.chars().count();
                        let pic = font.render(&[line], columns, text::Ink { rgb: NOTE_INK, backing: 0 });
                        note_tex = Some(unsafe { renderer.texture(&gl, pic.w as i32, pic.h as i32, &pic.rgba)? });
                    }
                }
            } else if let Some(old) = note_tex.take() {
                unsafe { renderer.delete(&gl, old) };
            }
        }
        let overlays: Vec<(&render::Texture, i32)> =
            overlay_tex.iter().map(|t| (t, 8)).chain(note_tex.iter().map(|t| (t, 4))).collect();
        let scene = Scene {
            blank: panel_blank,
            amount: panel_amount,
            eyes: pose.uniforms(),
            look: &look,
            panel: panel_tex.as_ref(),
            overlays: &overlays,
        };

        if let Some(dir) = &args.frames {
            let (fw, fh) = args.size;
            let px = unsafe { render::capture(&gl, fw, fh, || scene.draw(&gl, &mut renderer, fw as i32, fh as i32, 1))? };
            write_png(&dir.join(format!("frame-{frame_no:05}.png")), fw, fh, &px)?;
        }
        frame_no += 1;

        let mut drew = false;
        let mut drawing = Duration::ZERO;
        if !hidden && !(panel_blank && blank_presented) {
            unsafe { scene.draw(&gl, &mut renderer, w, h, scale)? };
            drawing = frame_start.elapsed();
            window.gl_swap_window();
            drew = true;
            blank_presented = panel_blank;
            if let Some(link) = &mut link {
                link.presented();
            }
        }
        drop(overlays);
        if let Some(link) = &mut link {
            if panel_blank {
                // Nothing heard is kept while the screen is dark.
                link.side.captions.forget();
            }
        }

        // Draw only as often as what is on screen needs (task 2.1).
        rate = pace::hz(anim.activity(), software);
        if panel_amount > 0.0 && panel_amount < 1.0 {
            rate = pace::hz(pace::Activity::Active, software);
        } else if shown.is_some() {
            rate = rate.max(pace::hz(pace::Activity::Calm, software));
        }
        let work = frame_start.elapsed();
        if drew {
            frame_ms = if frame_ms == 0.0 { work.as_secs_f64() * 1e3 } else { 0.9 * frame_ms + 0.1 * work.as_secs_f64() * 1e3 };
        }
        stats.frame(work, drawing, drew, rate);
        if let Some(line) = stats.report(Instant::now()) {
            eprintln!("{line}");
        }
        if recording {
            deadline = None;
        } else if !vsync || fast_display || software || rate < 60 {
            deadline = Some(pace::wait(deadline, frame_start, rate));
        } else {
            deadline = None;
        }
    }

    if power.restore() {
        if let Some(fd) = dpms::sdl_drm_fd(&window) {
            let _ = connectors.set(fd, dpms::DPMS_ON);
        }
    }
    if let Some(path) = &args.record {
        let (w, h) = args.size;
        let overlays: Vec<(&render::Texture, i32)> = overlay_tex.iter().map(|t| (t, 8)).collect();
        let scene = Scene {
            blank: anim.panel_blank(),
            amount: panel_amount,
            eyes: pose.uniforms(),
            look: &look,
            panel: panel_tex.as_ref(),
            overlays: &overlays,
        };
        let px = unsafe { render::capture(&gl, w, h, || scene.draw(&gl, &mut renderer, w as i32, h as i32, 1))? };
        write_png(path, w, h, &px)?;
        println!("recorded {} (showing: {})", path.display(), anim.target_name());
    }
    Ok(())
}

/// Everything one frame shows: the eyes (full screen, or gliding into the
/// strip by `amount`), the app panel, and overlays anchored bottom left.
struct Scene<'a> {
    blank: bool,
    amount: f32,
    eyes: [f32; 32],
    look: &'a render::Look,
    panel: Option<&'a render::Texture>,
    /// Each overlay with its margin from the bottom-left corner.
    overlays: &'a [(&'a render::Texture, i32)],
}

impl Scene<'_> {
    /// Draw into the bound framebuffer, `w` by `h`.
    ///
    /// # Safety
    /// Needs the renderer's GL context to be current.
    unsafe fn draw(&self, gl: &glow::Context, renderer: &mut render::Renderer, w: i32, h: i32, scale: i32) -> Result<(), String> {
        if self.blank {
            render::blank(gl);
            return Ok(());
        }
        if self.amount > 0.0 {
            let layout = panel::layout(w, h, self.amount);
            render::blank(gl);
            renderer.draw_scaled_in(gl, layout.eyes, scale, &self.eyes, self.look)?;
            if let Some(tex) = self.panel {
                renderer.draw_texture(gl, tex, layout.panel, w, h, layout.panel_alpha);
            }
        } else {
            renderer.draw_scaled(gl, w, h, scale, &self.eyes, self.look)?;
        }
        let zoom = (h / 800).max(1);
        for &(tex, margin) in self.overlays {
            let rect = panel::Rect { x: margin * zoom, y: margin * zoom, w: tex.w * zoom, h: tex.h * zoom };
            renderer.draw_texture(gl, tex, rect, w, h, 1.0);
        }
        Ok(())
    }
}

/// Marks that the birth ceremony has played this boot (the appliance only).
const BIRTH_MARKER: &str = "/run/being-face-born";
/// How long a software renderer's note stays on screen.
const SOFTWARE_NOTE: Duration = Duration::from_secs(30);
/// Overlay text: the eyes' own cyan, so it never reads as boot text.
const CAPTION_INK: [u8; 3] = [120, 215, 255];
/// The software note: dim enough (r+g+b at most 110) that the lab never
/// counts it as lit.
const NOTE_INK: [u8; 3] = [10, 42, 58];

/// A verbose boot (`being.verbose=1` on the kernel command line) keeps the
/// kernel's and systemd's messages readable: the face (and the Try menu, in
/// being-console.c) waits before it takes the screen. Esc, F2, Enter or Space
/// brings it up at once. `being.verbose_hold=SECONDS` changes the wait, and
/// `being.verbose_hold=0` waits for a key however long that takes. The wait
/// happens once per boot: whoever ends it leaves RELEASED behind.
#[cfg(any(target_os = "linux", test))]
mod verbose {
    use std::time::Duration;

    pub const DEFAULT_HOLD: Duration = Duration::from_secs(20);
    #[cfg(target_os = "linux")]
    const RELEASED: &str = "/run/being-verbose-released";

    /// None: not a verbose boot. Some(None): wait for a key. Some(Some(d)):
    /// wait at most d.
    pub fn hold_from_cmdline(cmdline: &str) -> Option<Option<Duration>> {
        let mut verbose = false;
        let mut hold = Some(DEFAULT_HOLD);
        for word in cmdline.split_ascii_whitespace() {
            match word.split_once('=') {
                Some(("being.verbose", value)) => verbose = value == "1",
                Some(("being.verbose_hold", value)) => {
                    if let Ok(secs) = value.parse::<u64>() {
                        hold = (secs > 0).then(|| Duration::from_secs(secs.min(3600)));
                    }
                }
                _ => {}
            }
        }
        verbose.then_some(hold)
    }

    #[cfg(target_os = "linux")]
    pub fn hold_for_boot_messages(who: &str) {
        use std::fs::{File, OpenOptions};
        use std::io::{ErrorKind, Read, Write};
        use std::os::unix::fs::OpenOptionsExt;
        use std::path::{Path, PathBuf};
        use std::time::Instant;

        let Ok(cmdline) = std::fs::read_to_string("/proc/cmdline") else { return };
        let Some(hold) = hold_from_cmdline(&cmdline) else { return };
        let released = Path::new(RELEASED);
        if released.exists() {
            return;
        }
        let note = match hold {
            Some(d) => format!(
                "being-{who}: verbose boot: the screen stays on these messages for {} s; press Esc or F2 to go on now",
                d.as_secs()
            ),
            None => format!("being-{who}: verbose boot: the screen stays on these messages; press Esc or F2 to go on"),
        };
        eprintln!("{note}");
        if let Ok(mut tty) = OpenOptions::new().write(true).open("/dev/tty0") {
            let _ = writeln!(tty, "\r\n{note}\r");
        }

        // struct input_event: a struct timeval (two longs), then u16 type,
        // u16 code and i32 value.
        let long = std::mem::size_of::<isize>();
        let size = 2 * long + 8;
        const O_NONBLOCK: i32 = 0o4000;
        const EV_KEY: u16 = 1;
        const KEYS: [u16; 5] = [1, 28, 57, 60, 96]; // Esc, Enter, Space, F2, keypad Enter
        let mut devices: Vec<(PathBuf, File)> = Vec::new();
        let mut scanned: Option<Instant> = None;
        let started = Instant::now();
        let mut buf = vec![0u8; size * 64];
        let pressed = 'wait: loop {
            if hold.is_some_and(|d| started.elapsed() >= d) || released.exists() {
                break false;
            }
            // Keyboards can turn up late (USB), so look again now and then.
            if scanned.map_or(true, |at| at.elapsed() >= Duration::from_secs(1)) {
                scanned = Some(Instant::now());
                if let Ok(entries) = std::fs::read_dir("/dev/input") {
                    for entry in entries.flatten() {
                        let path = entry.path();
                        let is_event = path.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("event"));
                        if is_event && !devices.iter().any(|(p, _)| *p == path) {
                            if let Ok(file) = OpenOptions::new().read(true).custom_flags(O_NONBLOCK).open(&path) {
                                devices.push((path, file));
                            }
                        }
                    }
                }
            }
            let mut gone = Vec::new();
            for (index, (_, file)) in devices.iter_mut().enumerate() {
                loop {
                    match file.read(&mut buf) {
                        Ok(0) => break,
                        Ok(n) => {
                            for event in buf[..n].chunks_exact(size) {
                                let kind = u16::from_ne_bytes([event[2 * long], event[2 * long + 1]]);
                                let code = u16::from_ne_bytes([event[2 * long + 2], event[2 * long + 3]]);
                                let value = i32::from_ne_bytes(event[2 * long + 4..2 * long + 8].try_into().unwrap());
                                if kind == EV_KEY && value == 1 && KEYS.contains(&code) {
                                    break 'wait true;
                                }
                            }
                        }
                        Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                        Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                        Err(_) => {
                            gone.push(index);
                            break;
                        }
                    }
                }
            }
            for index in gone.into_iter().rev() {
                devices.remove(index);
            }
            std::thread::sleep(Duration::from_millis(100));
        };
        eprintln!("being-{who}: {}", if pressed { "a key ended the verbose wait" } else { "verbose wait over" });
        let _ = File::create(released);
    }
}

const SCRIPTS: &[&str] = &["tour", "drowse", "birth"];

/// `--marks glint,ring,ears,network,ssh` (any of them).
fn parse_marks(list: &str) -> Result<render::Marks, String> {
    let mut marks = render::Marks::default();
    for name in list.split(',').filter(|n| !n.is_empty()) {
        match name {
            "glint" => marks.glint = 1.0,
            "ring" => marks.ring = 1.0,
            "ears" => marks.ears = 1.0,
            "network" => marks.network = 1.0,
            "ssh" => marks.ssh = 1.0,
            other => return Err(format!("--marks: no mark called {other} (glint, ring, ears, network, ssh)")),
        }
    }
    Ok(marks)
}

/// A made-up voice: syllables in phrases, with breaths between them.
fn voice_level(t: f64) -> f32 {
    let phrase = (t * 0.55).fract();
    if phrase > 0.82 {
        return 0.0;
    }
    let syllable = (t * 7.3).sin().abs() * (0.6 + 0.4 * (t * 2.1).sin());
    syllable.clamp(0.0, 1.0) as f32
}

/// Play a scripted scene: fire whatever falls in (from, to]. Returns extra
/// seconds to let pass silently before the next frame (a fast-forward).
fn scene(anim: &mut anim::Anim, name: &str, from: f64, to: f64) -> f32 {
    let at = |t: f64| from < t && t <= to || (t == 0.0 && to == 0.0);
    let during = |a: f64, b: f64| a <= to && to < b;
    match name {
        "tour" => {
            // Someone sits down in front of it, talks, and leaves it be.
            if during(2.5, 22.0) && (to * 2.5).floor() != (from * 2.5).floor() {
                anim.set_gaze(0.22, 0.05);
            }
            let states = [
                (3.5, "attending"),
                (5.0, "listening"),
                (7.5, "thinking"),
                (10.6, "speaking"),
                (14.6, "attending"),
                (15.6, "idle"),
                (30.0, "drowsy"),
                (34.5, "asleep"),
            ];
            for (t, state) in states {
                if at(t) {
                    anim.set_state(state, false, false);
                }
            }
            if at(10.5) {
                anim.express("joy", 30.0);
            }
            if during(10.6, 14.6) {
                anim.set_level(voice_level(to));
            }
            if at(14.6) {
                anim.end_reply();
            }
            if at(16.5) {
                anim.express("curiosity", 3.0);
            }
            if at(20.0) {
                anim.express("surprise", 2.0);
            }
            if at(22.0) {
                anim.clear_gaze();
            }
            if at(23.0) {
                anim.perform("content");
            }
            if at(26.0) {
                anim.perform("survey");
            }
        }
        "birth" => {
            // The very first power-on.
            if at(0.3) {
                anim.born();
            }
        }
        "drowse" => {
            // Seven quiet minutes pass in a moment; then someone comes by.
            if at(1.0) {
                return 420.0;
            }
            if during(9.0, 12.0) && (to * 2.5).floor() != (from * 2.5).floor() {
                anim.set_gaze(-0.3, 0.0);
            }
        }
        _ => {}
    }
    0.0
}

/// Render still images and exit: `--snapshot` and `--sheet`.
fn stills(gl: &glow::Context, renderer: &render::Renderer, look: &render::Look, args: &Args) -> Result<(), String> {
    if let Some(path) = &args.snapshot {
        let pose = eye::preset(&args.expr).ok_or(format!("no expression called {}", args.expr))?;
        let (w, h) = args.size;
        let px = unsafe { renderer.snapshot(gl, w, h, &pose.uniforms(), look)? };
        write_png(path, w, h, &px)?;
        println!("wrote {} ({}, {w}x{h})", path.display(), args.expr);
    }
    if let Some(path) = &args.sheet {
        let names: Vec<&str> = if args.names.is_empty() {
            eye::SHEET.to_vec()
        } else {
            args.names.iter().map(String::as_str).collect()
        };
        // Each tile is a small 16:10 screen (or --size), three across with
        // a thin gap.
        let (tw, th) = if args.size_given { args.size } else { (640u32, 400u32) };
        let gap = 8u32;
        let cols = 3u32.min(names.len() as u32).max(1);
        let rows = (names.len() as u32).div_ceil(cols);
        let (sw, sh) = (cols * tw + (cols + 1) * gap, rows * th + (rows + 1) * gap);
        let mut sheet = vec![0u8; (sw * sh * 4) as usize];
        for px in sheet.chunks_exact_mut(4) {
            px.copy_from_slice(&[58, 54, 52, 255]);
        }
        for (i, name) in names.iter().enumerate() {
            let pose = eye::preset(name).ok_or(format!("no expression called {name}"))?;
            let tile = unsafe { renderer.snapshot(gl, tw, th, &pose.uniforms(), look)? };
            let (col, row) = (i as u32 % cols, i as u32 / cols);
            let (ox, oy) = (gap + col * (tw + gap), gap + row * (th + gap));
            for y in 0..th {
                let src = (y * tw * 4) as usize;
                let dst = (((oy + y) * sw + ox) * 4) as usize;
                sheet[dst..dst + (tw * 4) as usize].copy_from_slice(&tile[src..src + (tw * 4) as usize]);
            }
        }
        write_png(path, sw, sh, &sheet)?;
        println!("wrote {} ({})", path.display(), names.join(", "));
    }
    Ok(())
}

fn write_png(path: &std::path::Path, w: u32, h: u32, rgba: &[u8]) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        if !dir.as_os_str().is_empty() {
            std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        }
    }
    let file = std::fs::File::create(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut encoder = png::Encoder::new(std::io::BufWriter::new(file), w, h);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
    writer.write_image_data(rgba).map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::verbose::{hold_from_cmdline, DEFAULT_HOLD};
    use std::time::Duration;

    #[test]
    fn only_a_verbose_boot_holds_the_screen() {
        let try_boot = "root=PARTUUID=x ro being.try=1 console=ttyS0,115200 loglevel=3";
        assert_eq!(hold_from_cmdline(try_boot), None);
        assert_eq!(hold_from_cmdline("being.verbose=0"), None);
        assert_eq!(hold_from_cmdline("being.verbose"), None);
        assert_eq!(hold_from_cmdline(&format!("{try_boot} being.verbose=1")), Some(Some(DEFAULT_HOLD)));
        assert_eq!(hold_from_cmdline("being.verbose_hold=45 being.verbose=1"), Some(Some(Duration::from_secs(45))));
        assert_eq!(hold_from_cmdline("being.verbose=1 being.verbose_hold=0"), Some(None));
        assert_eq!(hold_from_cmdline("being.verbose=1 being.verbose_hold=soon"), Some(Some(DEFAULT_HOLD)));
    }
}
