//! The face: two glowing eyes drawn straight to the screen.
//!
//! On the laptop it runs full-screen on KMS/DRM with no window system
//! (SDL_VIDEODRIVER=kmsdrm). On a development Mac it runs in a window.

mod anim;
mod audition;
mod chrome;
mod colour;
mod decor;
mod dpms;
mod emote;
mod eye;
mod keys;
mod link;
mod look;
mod options;
mod overlay;
mod pace;
mod panel;
mod pointer_probe;
mod quick_menu;
mod render;
mod status;
mod text;
mod viseme;
mod widgets;

use bus::Event;
use eye::ExprId;
use sdl2::event::{Event as SdlEvent, WindowEvent};
use sdl2::keyboard::{Keycode, Mod};
use sdl2::mouse::MouseButton;
use sdl2::video::GLProfile;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const HELP: &str = "\
face: the being's eyes

  face                         connect to the brainstem and show its state
  face --demo                  cycle through expressions (all poses with --full)
  face --standalone            idle on its own, no brainstem needed
  face --fullscreen            cover the screen (always the case on the laptop)
  face --size 1280x800         window size
  face --scale N               draw at 1/N size and stretch (default: 4 on a
                               software renderer, otherwise 1)
  face --bus PATH              bus socket (default: see docs/PROTOCOL.md)
  face --list                  print the expression names
  face --list-auditions        print the additional Face Lab expression names
  face --snapshot FILE.png [--expr NAME] [--size WxH]    render one still image
  face --sheet FILE.png [--names a,b,c|all] [--size WxH] render a sheet of stills
                               (each tile is WxH if given, else 640x400)

For tests: --hidden shows no window, --exit-after SECS quits by itself, and
--record FILE.png saves what the face looked like at the moment it quit.

For reviewing motion off-screen:
  face --audition NAME [--intensity 0..1.5] [--gesture NAME]
                               play an expression and an optional gesture
  face --blend NAME [--mix 0..1]           blend with the audition expression
  face --list-gestures                     list all Face Lab actions
  face --list-emotes                       list all effects
  face --emote NAME[,NAME...]              add effects to a preview
  face --say TEXT                          preview silent mouth articulation
  face --source-pose                       use the Face Lab's authored eye geometry
  face --follow-cursor                    track the pointer in a visual preview
  face --pointer-probe --standalone --fullscreen --exit-after SECS
                               bounded KMS SDL pointer evidence (max 60 s)
  face --script NAME           play a scripted scene instead of the brainstem:
                               tour (wake, attention, thought, speech, emotions,
                               idle life, a yawn, sleep), drowse, birth, or a
                               Face Lab scene: wake-up, jump-scare, heartbreak,
                               plot-twist, meltdown, flirt, boot-up, bad-news,
                               cold-night, motion-arm-a, motion-arm-b,
                               motion-arm-c, motion-arm-d
  face --frames DIR [--fps N]  run on a fixed clock with no window and save
                               every frame as DIR/frame-00000.png (default 30
                               fps); pair with --audition, --demo or --script, --size and
                               --exit-after, which then counts scene seconds
                               (default: the end of the scene).
                               ffmpeg -framerate 30 -i DIR/frame-%05d.png out.mp4
  face --marks glint,ring,ears,network,ssh   show privacy marks (stills, frames)
  face --brows --mouth --details --effects   enable individual face layers
  face --full                  enable all five optional face layers
  face --style NAME            eyes, eyes+brows, eyes+mouth, expressive, full
  face --layers LIST           choose brows,mouth,details,effects,colour
  face --mood-colour           use expression-specific eye colours
  face --colour NAME           choose a fixed eye colour
  face --list-colours          list available eye colours
  face --seed N                the same idle life every run (blinks, glances and
                               gestures), for reproducible frame dumps

Keys: Esc interrupts it, M mutes it, F1 shows captions (off by default and
never logged), Shift+F1 shows health, F2 opens appliance setup, F6 or Command+Space opens the Quick Menu, F9 recovers a face with no lid reading, F10
mutes it, and Q closes the window. On the laptop the top row does what is printed on it
(hold fn for F1 to F12): brightness, Mission Control (as F2), Launchpad (the terminal),
keyboard light, play/pause (listen, or stop a reply), volume and speaker mute; a bar
shows the new level. Space, Return or a click while the eyes rest asks for its
attention, as \"Hey Pip\" does. With the panel dark and the brainstem silent, any key
shows the face. While an app panel has focus, keys go to it (M is a letter then; F10
still mutes, and the top row's keys are never typed).

Full screen, the face shows itself after a few seconds with no lid reading
(waiting calmly for the first minute, then dazed if the brainstem is gone), turns
the panel's connector off while dark (BEING_FACE_DPMS=0 leaves it on), and draws
at 60, 30, 15 or 5 frames a second as what it shows needs. With being.verbose=1 on the kernel command
line it first leaves the boot messages up for 20 s (being.verbose_hold=SECS,
0 to wait for a key); Esc, Enter, Space, F2 or Mission Control ends the wait.";

struct Args {
    bus: PathBuf,
    fullscreen: bool,
    size: (u32, u32),
    /// --size was given (a sheet then uses it for each tile).
    size_given: bool,
    demo: bool,
    standalone: bool,
    list: bool,
    list_auditions: bool,
    list_gestures: bool,
    list_emotes: bool,
    list_colours: bool,
    audition: Option<ExprId>,
    blend: Option<ExprId>,
    mix: f32,
    speech: Option<viseme::Visemes>,
    source_pose: bool,
    follow_cursor: bool,
    pointer_probe: bool,
    manual_emotes: [bool; emote::CHANNELS],
    intensity: f32,
    gesture: Option<String>,
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
    preview_brows: bool,
    preview_mouth: bool,
    preview_details: bool,
    effects: bool,
    full: bool,
    override_layers: bool,
    mood_colour: bool,
    palette: Option<colour::Palette>,
}

impl Args {
    fn select_layers(&mut self, [brows, mouth, details, effects, colour]: [bool; 5]) {
        self.preview_brows = brows;
        self.preview_mouth = mouth;
        self.preview_details = details;
        self.effects = effects;
        self.full = brows && mouth && details && effects && colour;
        self.mood_colour = colour && !self.full;
        self.override_layers = true;
    }

    fn demo_catalogue(&self) -> &'static [ExprId] {
        if self.full { ExprId::ALL } else { ExprId::PUBLIC }
    }
}

fn parse_args(mut it: impl Iterator<Item = String>) -> Result<Args, String> {
    let mut a = Args {
        bus: bus::socket_path(),
        fullscreen: false,
        size: (1280, 800),
        size_given: false,
        demo: false,
        standalone: false,
        list: false,
        list_auditions: false,
        list_gestures: false,
        list_emotes: false,
        list_colours: false,
        audition: None,
        blend: None,
        mix: 0.5,
        speech: None,
        source_pose: false,
        follow_cursor: false,
        pointer_probe: false,
        manual_emotes: [false; emote::CHANNELS],
        intensity: 1.0,
        gesture: None,
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
        preview_brows: false,
        preview_mouth: false,
        preview_details: false,
        effects: false,
        full: false,
        override_layers: false,
        mood_colour: false,
        palette: None,
    };
    let mut intensity_given = false;
    let mut mix_given = false;
    while let Some(arg) = it.next() {
        let mut value = |name: &str| it.next().ok_or(format!("{name} needs a value"));
        match arg.as_str() {
            "--bus" => a.bus = PathBuf::from(value("--bus")?),
            "--fullscreen" => a.fullscreen = true,
            "--demo" => a.demo = true,
            "--standalone" => a.standalone = true,
            "--list" => a.list = true,
            "--list-auditions" => a.list_auditions = true,
            "--list-gestures" => a.list_gestures = true,
            "--list-emotes" => a.list_emotes = true,
            "--list-colours" | "--list-colors" => a.list_colours = true,
            "--audition" => {
                let name = value("--audition")?;
                a.audition = Some(ExprId::from_name(&name).ok_or_else(|| format!("no expression called {name}"))?);
            }
            "--blend" => {
                let name = value("--blend")?;
                a.blend = Some(ExprId::from_name(&name).ok_or_else(|| format!("no expression called {name}"))?);
            }
            "--mix" => {
                a.mix = value("--mix")?.parse().map_err(|_| "--mix wants a number")?;
                if !(0.0..=1.0).contains(&a.mix) {
                    return Err("--mix must be between 0 and 1".to_string());
                }
                mix_given = true;
            }
            "--source-pose" => a.source_pose = true,
            "--follow-cursor" => a.follow_cursor = true,
            "--pointer-probe" => a.pointer_probe = true,
            "--say" => {
                let text = value("--say")?;
                if text.trim().is_empty() || text.chars().count() > 280 {
                    return Err("--say wants 1 to 280 characters".to_string());
                }
                a.speech = Some(viseme::Visemes::new(&text));
                a.preview_mouth = true;
            }
            "--emote" => {
                let names = value("--emote")?;
                for name in names.split(',').map(str::trim) {
                    let channel = emote::channel(name).ok_or_else(|| format!("unknown emote {name:?}"))?;
                    a.manual_emotes[channel] = true;
                }
                a.effects = true;
            }
            "--colour" | "--color" => {
                let name = value("--colour")?;
                a.palette = Some(colour::Palette::from_name(&name)
                    .ok_or_else(|| format!("--colour wants one of {}", colour::Palette::NAMES.join(", ")))?);
            }
            "--mood-colour" | "--mood-color" => a.mood_colour = true,
            "--intensity" => {
                a.intensity = value("--intensity")?.parse().map_err(|_| "--intensity wants a number")?;
                if !(0.0..=1.5).contains(&a.intensity) {
                    return Err("--intensity must be between 0 and 1.5".to_string());
                }
                intensity_given = true;
            }
            "--gesture" => {
                let name = value("--gesture")?;
                if audition::Gesture::from_name(&name).is_none() {
                    return Err(format!("--gesture wants one of {}", audition::Gesture::NAMES.join(", ")));
                }
                a.gesture = Some(name);
            }
            "--hidden" => a.hidden = true,
            "--brows" => a.preview_brows = true,
            "--mouth" => a.preview_mouth = true,
            "--details" => a.preview_details = true,
            "--effects" => a.effects = true,
            "--full" => a.select_layers([true; 5]),
            "--style" => {
                let style = value("--style")?;
                let layers = match style.as_str() {
                    "eyes" => [false; 5],
                    "eyes+brows" => [true, false, false, false, false],
                    "eyes+mouth" => [false, true, false, false, false],
                    "expressive" => [true, true, false, false, false],
                    "full" => [true; 5],
                    _ => return Err("--style wants eyes, eyes+brows, eyes+mouth, expressive, or full".to_string()),
                };
                a.select_layers(layers);
            }
            "--layers" => {
                let list = value("--layers")?;
                let mut layers = [false; 5];
                for name in list.split(',').map(str::trim) {
                    let index = match name {
                        "brows" => 0,
                        "mouth" => 1,
                        "details" | "eye_details" => 2,
                        "effects" => 3,
                        "colour" | "color" | "tint" => 4,
                        _ => return Err(format!("unknown face layer {name:?}")),
                    };
                    layers[index] = true;
                }
                a.select_layers(layers);
            }
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
                if !secs.is_finite() || secs < 0.0 {
                    return Err("--exit-after wants finite, non-negative seconds".to_string());
                }
                a.exit_after = Some(secs);
            }
            "--script" => {
                let name = value("--script")?;
                if !SCRIPTS.contains(&name.as_str()) && !audition::SCENES.contains(&name.as_str()) {
                    let names = [SCRIPTS, audition::SCENES].concat();
                    return Err(format!("--script wants one of {}", names.join(", ")));
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
    a.mood_colour |= a.full && a.palette.is_none();
    a.effects |= a.manual_emotes.iter().any(|on| *on);
    a.preview_mouth |= a.speech.is_some();
    if mix_given && a.blend.is_none() {
        return Err("--mix requires --blend".to_string());
    }
    if a.speech.is_some() && (a.sheet.is_some() || a.snapshot.is_some()) {
        return Err("--say needs an animated preview".to_string());
    }
    let explicit_exit = a.exit_after.is_some();
    if a.pointer_probe {
        if !a.fullscreen || !a.standalone {
            return Err("--pointer-probe requires --fullscreen and --standalone".to_string());
        }
        if !explicit_exit || !a.exit_after.is_some_and(|secs| secs > 0.0 && secs <= 60.0) {
            return Err("--pointer-probe requires --exit-after greater than zero and at most 60 seconds".to_string());
        }
        if a.hidden || a.frames.is_some() || a.snapshot.is_some() || a.sheet.is_some() {
            return Err("--pointer-probe requires a visible live face".to_string());
        }
    }
    if a.audition.is_some() {
        if a.demo || a.script.is_some() || a.sheet.is_some() || a.snapshot.is_some() {
            return Err("--audition wants its own scene".to_string());
        }
        a.exit_after.get_or_insert(AUDITION_END);
    } else if intensity_given || a.gesture.is_some() || a.blend.is_some() {
        return Err("--intensity, --gesture and --blend require --audition".to_string());
    }
    if a.gesture.as_deref().and_then(audition::Gesture::from_name) == Some(audition::Gesture::StartleBurst) {
        a.effects = true;
    }
    if let Some(duration) = a.script.as_deref().and_then(audition::duration) {
        a.exit_after.get_or_insert(duration);
    }
    if !explicit_exit {
        if let Some(speech) = &a.speech {
            let start = if a.audition.is_some() { AUDITION_START } else { 0.45 };
            a.exit_after = Some(a.exit_after.unwrap_or(0.0).max(start + speech.duration() + 0.3));
        } else if a.manual_emotes.iter().any(|on| *on) && a.audition.is_none() && a.script.is_none() && !a.demo {
            a.exit_after = Some(AUDITION_END);
        }
    }
    // Saved frames never end on their own; stop a scene at its natural end
    // rather than filling the disk.
    if a.frames.is_some() && a.exit_after.is_none() {
        a.exit_after = Some(match a.script.as_deref() {
            Some("tour") => 38.0,
            Some("drowse") => 16.0,
            Some("birth") => 9.0,
            _ if a.demo => 2.5 + 2.4 * a.demo_catalogue().len() as f64,
            _ => return Err("--frames needs --audition, --script, --demo or --exit-after".to_string()),
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
    let process_started = Instant::now();
    let args = parse_args(std::env::args().skip(1))?;
    if args.list || args.list_auditions {
        for id in if args.list_auditions { ExprId::AUDITIONS } else { ExprId::PUBLIC } {
            println!("{}", id.name());
        }
        return Ok(());
    }
    for names in [
        args.list_gestures.then_some(audition::Gesture::NAMES),
        args.list_emotes.then_some(&emote::NAMES[..]),
        args.list_colours.then_some(colour::Palette::NAMES),
    ].into_iter().flatten() {
        println!("{}", names.join("\n"));
    }
    if args.list_gestures || args.list_emotes || args.list_colours {
        return Ok(());
    }
    let offscreen = args.snapshot.is_some() || args.sheet.is_some();
    // systemd's watchdog (WatchdogSec= in being-face.service): a frame loop
    // that stops turning is restarted. Every frame kicks it; the notifier
    // sends at most a few datagrams per interval and never blocks.
    let mut watchdog = if offscreen { bus::notify::Notifier::none() } else { bus::notify::Notifier::from_env() };
    #[cfg(target_os = "linux")]
    if args.fullscreen && !offscreen && !args.hidden && args.frames.is_none() {
        verbose::hold_for_boot_messages("face", &mut || watchdog.kick());
    }

    let sdl = sdl2::init()?;
    let video = sdl.video()?;
    let (window, _ctx) = make_window(&video, &args, offscreen || args.hidden || args.frames.is_some())?;
    let text_input = video.text_input();
    text_input.stop();
    let gl = unsafe { glow::Context::from_loader_function(|s| video.gl_get_proc_address(s) as *const _) };
    let mut renderer = unsafe { render::Renderer::new(&gl)? };
    let effects_enabled = args.effects || (!args.override_layers
        && std::env::var("BEING_FACE_EFFECTS").as_deref() == Ok("1"));
    // Optional layers never take the live face down: a driver that rejects
    // their shaders leaves it eyes-only. Stills fail instead, so a lab
    // capture never silently lacks the layer it asked for.
    if effects_enabled {
        if let Err(error) = unsafe { renderer.enable_emotes(&gl) } {
            if offscreen {
                return Err(error);
            }
        }
    }
    let mut look = render::Look::default();
    let mut decor = decor::Decor::default();
    let mut mood = colour::Mood::default();
    look.accent_emotes = accent_emotes(effects_enabled, args.mood_colour);

    if let Some(marks) = args.marks {
        look.marks = marks;
    }
    if offscreen {
        if args.preview_brows || args.preview_mouth || args.preview_details || effects_enabled {
            unsafe { renderer.enable_decor(&gl)? };
        }
        return stills(&gl, &renderer, &look, &args, effects_enabled);
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
    let mut pointer_probe = args.pointer_probe.then(pointer_probe::Probe::default);
    let local_setup = cfg!(target_os = "linux") && std::env::var("BEING_LOCAL_SETUP").as_deref() == Ok("1");
    let mut setup_start = Launcher::default();
    let appliance = cfg!(target_os = "linux") && args.fullscreen && !hidden;
    if appliance && std::env::var("MESA_SHADER_CACHE_DIR").as_deref() == Ok("/mind/display-cache") {
        eprintln!("face: installed shader cache selected");
    }
    let cmdline = appliance.then(|| std::fs::read_to_string("/proc/cmdline").ok()).flatten();
    let idle_trial = cmdline.as_deref().and_then(pace::idle_trial);
    if let Some(rate) = idle_trial {
        eprintln!("face: idle pacing experiment {rate} Hz");
    }
    let skip_idle_swaps = !software && !args.standalone && !args.demo && args.script.is_none()
        && args.audition.is_none() && !args.follow_cursor && video.current_video_driver().eq_ignore_ascii_case("kmsdrm")
        && cmdline.as_deref().is_some_and(|line| pace::try_switch(line, "being.face_present_on_change"));
    if skip_idle_swaps {
        eprintln!("face: present-on-change experiment on the Try panel");
    }

    let seed = args.seed.unwrap_or_else(|| {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(1)
    });
    let mut anim = anim::Anim::new(seed);
    if cmdline.as_deref().is_some_and(|line| pace::try_switch(line, "being.face_reply_ladder")) {
        anim.enable_reply_ladder();
        eprintln!("face: speech-end reply ladder experiment");
    }
    if cmdline.as_deref().is_some_and(|line| line.split_ascii_whitespace().any(|word| word == "being.body_breath=1")) {
        anim.enable_effort_breathing();
        eprintln!("face: body effort breathing experiment");
    }
    let concept_scene = args.script.as_deref().is_some_and(|name| audition::SCENES.contains(&name));
    let manual_effects = args.manual_emotes.iter().any(|on| *on);
    let direct_preview = manual_effects || args.speech.is_some() || args.follow_cursor;
    if args.audition.is_some() || concept_scene || direct_preview || args.source_pose || (args.demo && args.full) {
        anim.enable_concept_preview(args.manual_emotes, args.source_pose);
    }
    let offline_scene = args.demo || args.script.is_some() || args.audition.is_some() || direct_preview;
    let solo = args.standalone || offline_scene;
    // The appliance starts dark until the brainstem reports the lid, but not
    // for long: with no lid reading it shows itself (dazed if the brainstem
    // is gone) rather than leave a black panel.
    let lid_grace = args.fullscreen.then_some(link::LID_GRACE);
    let mut link = if solo { None } else { Some(link::Link::new(args.bus.clone(), lid_grace)) };
    let look_path = (!solo && appliance).then(|| {
        std::env::var_os("BEING_MIND").map_or_else(|| PathBuf::from("/mind"), PathBuf::from).join("look.json")
    });
    // The mount is ordered before the face on the appliance. Read the
    // durable look before the first frame, without waiting for the mind/bus.
    let mut prefs = look_path.as_ref()
        .map(|path| look::Prefs::startup(path, Path::new(look::MIRROR))).unwrap_or_default();
    prefs.palette(&mut look);
    let mut next_look_check = Instant::now();
    if let Some(link) = &mut link {
        link.side = link::Side::new(appliance.then(|| PathBuf::from(BIRTH_MARKER)));
        link.side.captions.set_enabled(prefs.captions);
    }
    if solo {
        anim.set_state("idle", false, false);
        if offline_scene {
            anim.set_lid(Some(false), false, concept_scene || (direct_preview && args.audition.is_none()));
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
    let mut panel = (!offline_scene).then(|| panel::Panel::new(
        std::env::var_os("BEING_PANEL_DIR").map_or_else(|| PathBuf::from(panel::DIR), PathBuf::from),
    ));
    let mut keys = panel::KeySender::new(std::env::var_os("BEING_HANDS_KEYS").map_or_else(|| PathBuf::from(panel::KEYS), PathBuf::from));
    let mut panel_tex: Option<render::Texture> = None;
    let mut lookup_texture = false;
    let mut menu_tex: Option<render::Texture> = None;
    let mut menu_painted = None;
    let mut options_tex: Option<render::Texture> = None;
    let mut options_painted = None;
    // The picture changed while hidden, so the texture is out of date.
    let mut panel_behind = false;
    let mut panel_amount = 0f32;
    let mut panel_focus = false;
    let mut previous_job = None;
    let mut widget_tiles = widgets::Tiles::default();
    let mut caption_tex: Option<render::Texture> = None;
    let mut caption_style = None;
    let mut saved_captions = prefs.captions;
    let mut health_open = false;
    let mut overlay_tex: Option<render::Texture> = None;
    let mut overlay_drawn: Option<Instant> = None;
    let mut night_notice: Option<(u64, render::Texture)> = None;
    // The level bar shown after a brightness, volume or keyboard-light change.
    let mut level_tex: Option<render::Texture> = None;
    // The text in level_tex, and how strongly it is drawn.
    let mut level_shown: Option<String> = None;
    let mut level_alpha = 1.0f32;
    // Top-row keys: held ones step at a steady pace; Mission Control and
    // Launchpad, which SDL drops, are read from the keyboards themselves.
    let mut repeats = keys::Repeats::default();
    let mut media: Vec<(keys::Media, bool)> = Vec::new();
    let (media_tx, media_rx) = std::sync::mpsc::channel::<keys::Media>();
    #[cfg(target_os = "linux")]
    if appliance && !solo {
        keys::watch(media_tx);
    }
    #[cfg(not(target_os = "linux"))]
    drop(media_tx);
    let mut note_tex: Option<render::Texture> = None;
    let mut stats = pace::Stats::new(Instant::now());
    let mut frame_ms = 0f64;
    let mut rate = 60u32;

    let mut demo_next = 2.5f64;
    let mut demo_index = 0usize;
    let started = Instant::now();
    let mut last = started;
    let mut pose = ExprId::Asleep.pose();
    let mut blank_presented = false;
    let mut displayed_idle = None;
    // A first swap can be black while the physical lid reading is pending.
    // Time the later visible frame separately, including GL setup above.
    let mut first_presented = false;
    let mut first_visible = false;
    let mut deadline: Option<Instant> = None;
    let mut frame_no = 0u64;
    let mut clock = 0f64;
    let mut skip = 0f32;

    'run: loop {
        let frame_start = Instant::now();
        watchdog.kick();
        setup_start.reap();
        let scene_time = if recording { clock } else { started.elapsed().as_secs_f64() };
        if args.exit_after.is_some_and(|secs| scene_time >= secs) {
            break 'run;
        }
        for event in events.poll_iter() {
            if matches!(&event, SdlEvent::Window { .. }) {
                displayed_idle = None;
            }
            match event {
                SdlEvent::Quit { .. } => break 'run,
                SdlEvent::Window { win_event: WindowEvent::DisplayChanged(display), .. } => {
                    fast_display = fast_refresh(display);
                }
                SdlEvent::KeyDown { keycode, scancode, keymod, repeat, .. } => {
                    // The top row's keys mean what is printed on them, even
                    // while an app panel has the keyboard: never typed.
                    if let Some(key) = scancode.and_then(keys::Media::from_scancode) {
                        media.push((key, repeat));
                        continue;
                    }
                    let Some(key) = keycode else { continue };
                    let command_space = key == Keycode::Space && keymod.intersects(Mod::LGUIMOD | Mod::RGUIMOD);
                    if key == Keycode::F6 || command_space {
                        if !repeat && !anim.panel_blank() && !anim.asleep() && prefs.menu_position != "off"
                            && !watch.get().guest
                        {
                            if let Some(link) = link.as_mut().filter(|link| !link.quiet()) {
                                link.toggle_menu();
                            }
                        }
                        continue;
                    }
                    // M is the plain keyboard's mic mute; it must still work
                    // while Options or the Quick Menu holds the keyboard.
                    if link.as_mut().is_some_and(|link| link.chrome_mute(key, repeat)) {
                        continue;
                    }
                    if let Some(link) = link.as_mut().filter(|link| link.side.options.open()) {
                        if ![Keycode::F1, Keycode::F2, Keycode::F9, Keycode::F10].contains(&key) {
                            let return_to_menu = key == Keycode::Escape && !repeat && link.side.options.opened_from_menu();
                            if let Some(change) = link.side.options.key(key, repeat, prefs, keymod) {
                                link.side.options.set_status(None);
                                link.ui_action(Event::new("ui.action").with("id", "shell.options.set")
                                    .with("field", change.field).with("value", change.value));
                            }
                            if return_to_menu && !anim.panel_blank() && !anim.asleep()
                                && prefs.menu_position != "off" && !watch.get().guest && !link.quiet()
                            {
                                link.side.menu.set_open(true);
                            }
                            continue;
                        }
                    }
                    if let Some(link) = link.as_mut().filter(|link| link.side.menu.open()) {
                        if ![Keycode::F1, Keycode::F2, Keycode::F9, Keycode::F10].contains(&key) {
                            match link.menu_key(key, repeat, true, frame_start) {
                                Some(quick_menu::Selection::App(id)) => {
                                    link.menu_action(id);
                                    panel_focus = false;
                                }
                                Some(selection @ (quick_menu::Selection::Options | quick_menu::Selection::Help
                                    | quick_menu::Selection::Setting(_))) => {
                                    link.side.options.open_from_menu();
                                    match selection {
                                        quick_menu::Selection::Help => link.side.options.show_help(),
                                        quick_menu::Selection::Setting(field) => link.side.options.focus_setting(field),
                                        _ => {}
                                    }
                                    link.menu_action("shell.options.open");
                                    panel_focus = false;
                                }
                                Some(quick_menu::Selection::Care) => {
                                    link.menu_action("shell.care.open");
                                    panel_focus = false;
                                }
                                None => {}
                            }
                            continue;
                        }
                    }
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
                    if ATTEND_KEYS.contains(&key) && asks_attention(&anim, panel_focus) {
                        if let Some(link) = &mut link {
                            link.send(Event::new("key.attend"));
                        }
                    } else if key == Keycode::Escape {
                        if let Some(link) = &mut link {
                            link.send(Event::new("key.interrupt"));
                        }
                    } else if key == Keycode::F1 {
                        if keymod.intersects(Mod::LSHIFTMOD | Mod::RSHIFTMOD) {
                            health_open = !health_open;
                            overlay_drawn = None;
                        } else if let Some(link) = &mut link {
                            link.side.captions.toggle();
                        }
                    } else if key == Keycode::M || key == Keycode::F10 {
                        if let Some(link) = &mut link {
                            link.send(Event::new("key.mute"));
                        }
                    } else if key == Keycode::F9 {
                        if let Some(link) = &mut link {
                            link.send(Event::new("key.panel_recover"));
                        }
                    } else if key == Keycode::F2 && local_setup {
                        setup_start.start("systemctl", &["--no-block", "start", "being-local-screen.service"]);
                    } else if key == Keycode::Q && !args.fullscreen {
                        break 'run;
                    }
                    if dark_and_alone {
                        if let Some(link) = &mut link {
                            link.unblank_locally(&mut anim);
                        }
                    }
                }
                SdlEvent::TextInput { text, .. } => {
                    if let Some(link) = link.as_mut().filter(|link| link.side.menu.searching()) {
                        link.side.menu.input(&text);
                    }
                }
                SdlEvent::MouseMotion { x, y, which, .. } if (args.follow_cursor || pointer_probe.is_some()) && which != u32::MAX => {
                    if let Some(probe) = &mut pointer_probe { probe.motion(x, y); }
                    if args.follow_cursor {
                        let (w, h) = window.size();
                        if w > 0 && h > 0 {
                            anim.set_gaze(2.0 * x as f32 / w as f32 - 1.0, 1.0 - 2.0 * y as f32 / h as f32);
                        }
                    }
                }
                SdlEvent::Window { win_event: WindowEvent::Leave, .. } if args.follow_cursor => anim.clear_gaze(),
                // SDL may synthesize a mouse event from the same touch, or
                // a touch from a mouse click. Ignore the synthetic copies.
                SdlEvent::MouseButtonDown { mouse_btn: MouseButton::Left, which, .. } if which != u32::MAX => {
                    if let Some(probe) = &mut pointer_probe { probe.mouse_click(); }
                    if offline_scene && !recording {
                        audition::Gesture::Poke.perform(&mut anim);
                    } else if let Some(link) = &mut link {
                        link.send(Event::new("key.touch"));
                        if !link.side.menu.open() && !link.side.options.open() && asks_attention(&anim, panel_focus) {
                            link.send(Event::new("key.attend"));
                        }
                    }
                }
                SdlEvent::FingerDown { touch_id, .. } if touch_id != -1 => {
                    if let Some(probe) = &mut pointer_probe { probe.finger_down(); }
                    if let Some(link) = &mut link {
                        link.send(Event::new("key.touch"));
                        if !link.side.menu.open() && !link.side.options.open() && asks_attention(&anim, panel_focus) {
                            link.send(Event::new("key.attend"));
                        }
                    }
                }
                _ => {}
            }
        }
        media.extend(media_rx.try_iter().map(|key| (key, false)));
        for (key, repeat) in media.drain(..) {
            if !repeats.admit(key, repeat, Instant::now()) {
                continue;
            }
            let dark_and_alone = anim.panel_blank() && link.as_ref().is_some_and(|l| l.quiet());
            let chrome_open = link.as_ref().is_some_and(|link| link.side.menu.open() || link.side.options.open());
            match keys::action(key, !chrome_open && asks_attention(&anim, panel_focus)) {
                keys::Action::Send(ev) => {
                    if let Some(link) = &mut link {
                        if key == keys::Media::Launchpad {
                            link.close_chrome();
                        }
                        link.send(ev);
                    }
                }
                keys::Action::Menu if local_setup => {
                    setup_start.start("systemctl", &["--no-block", "start", "being-local-screen.service"]);
                }
                keys::Action::Menu => eprintln!("face: the {} key opens setup only on the appliance", key.name()),
                keys::Action::Nothing(why) => eprintln!("face: the {} key does nothing: {why}", key.name()),
            }
            if dark_and_alone && !repeat {
                if let Some(link) = &mut link {
                    link.unblank_locally(&mut anim);
                }
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
                if concept_scene {
                    let previous = if frame_no == 0 { -1.0 } else { previous };
                    audition::advance(anim, name, previous, t);
                } else {
                    skip = scene(anim, name, previous, t);
                }
            }
            if let Some(id) = args.audition {
                if previous < AUDITION_START && t >= AUDITION_START {
                    let blend = args.blend.map(|other| (other, args.mix));
                    let (primary, blend) = match blend {
                        Some((other, 1.0)) => (other, None),
                        Some((_, 0.0)) => (id, None),
                        _ => (id, blend),
                    };
                    let hold = args.speech.as_ref().map_or(AUDITION_HOLD, |speech| AUDITION_HOLD.max(speech.duration()));
                    anim.audition_blend(primary, args.intensity, hold, blend);
                }
                if let Some(gesture) = &args.gesture {
                    if previous < AUDITION_GESTURE && t >= AUDITION_GESTURE {
                        if let Some(gesture) = audition::Gesture::from_name(gesture) {
                            gesture.perform(anim);
                        }
                    }
                }
            }
            if args.demo && t >= demo_next {
                let catalogue = args.demo_catalogue();
                let expression = catalogue[demo_index % catalogue.len()];
                demo_index += 1;
                demo_next = t + 2.4;
                match expression {
                    _ if args.full => anim.audition(expression, 1.0, 2.4),
                    ExprId::Asleep => anim.set_state("asleep", false, false),
                    ExprId::Dreaming => anim.preview_dreaming(),
                    ExprId::Muted => anim.set_state("idle", true, false),
                    ExprId::Dazed => anim.set_state("idle", false, true),
                    _ => {
                        anim.set_state("idle", false, false);
                        anim.express(expression.name(), 2.4);
                    }
                }
            }
        });
        if look_path.is_some() && now >= next_look_check {
            next_look_check = now + Duration::from_secs(1);
            if let Some(saved) = look::Prefs::read(Path::new(look::MIRROR)).filter(|saved| saved.revision > prefs.revision) {
                prefs = saved;
                prefs.palette(&mut look);
            }
        }
        if let Some(request) = link.as_ref().and_then(|link| link.side.look)
            .filter(|request| request.revision >= prefs.revision && *request != prefs)
        {
            prefs = request;
            prefs.palette(&mut look);
        }
        if prefs.captions != saved_captions {
            if let Some(link) = &mut link {
                link.side.captions.set_enabled(prefs.captions);
            }
            saved_captions = prefs.captions;
        }
        anim.set_idle_glances(prefs.idle_glances);
        let panel_blank = anim.panel_blank();
        let guest = watch.get().guest || link.as_ref().is_some_and(|link| link.side.menu.guest());
        let lookup_allowed = !panel_blank && !watch.get().guest
            && link.as_ref().is_some_and(|link| link.lookup_available(Instant::now()));
        if lookup_texture && !lookup_allowed {
            if let Some(old) = panel_tex.take() {
                unsafe { renderer.delete(&gl, old) };
            }
            lookup_texture = false;
            panel_behind = true;
        }
        // The workshop panel: read what is new, and let its focus steer the
        // eyes. Nothing is read while the screen is dark, and nothing is
        // uploaded while the face covers the panel in its sleep (the app's
        // own state does not change).
        let mut shown = None;
        let covered = anim.asleep();
        if let (Some(panel), false) = (&mut panel, panel_blank || guest) {
            let update = if link.as_ref().is_some_and(|link| link.side.menu.waiting()) {
                panel.poll_while_opening(now)
            } else {
                panel.poll(now)
            };
            let lookup_panel = panel.following().and_then(|path| path.file_stem()?.to_str()) == Some("lookup");
            let private_lookup = lookup_panel && !lookup_allowed;
            if let Some(header) = panel.shown(now).filter(|_| !covered && !private_lookup) {
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
                lookup_texture = lookup_panel;
            } else if private_lookup || update != panel::Update::Nothing {
                panel_behind = true;
            }
            let flags = panel.flags(now);
            anim.set_typing_hidden(shown.is_none() && flags & panel::flag::PIP_TYPING != 0);
        } else if let Some(panel) = &mut panel {
            // The whole app frame is read again when the panel lights.
            if !panel.pixels.is_empty() {
                panel.forget_picture();
            }
            // Close old pixels immediately while hands clears its own
            // surfaces. The menu must never expose the panel it covered.
            if guest {
                if let Some(old) = panel_tex.take() {
                    unsafe { renderer.delete(&gl, old) };
                }
                lookup_texture = false;
            }
        }
        let visible_app = shown.as_ref().and_then(|_| panel.as_ref()?.following()?.file_stem()?.to_str());
        let mut menu_position = None;
        let mut options_visible = false;
        let mut opening_app = false;
        let mut app_reaction = None;
        if let Some(link) = &mut link {
            link.side.menu.set_online(lookup_allowed);
            link.side.menu.set_care_allowed(local_setup);
            if let Some(position) = quick_menu::Position::parse(prefs.menu_position) {
                link.side.menu.position = position;
            } else {
                link.side.menu.set_open(false);
            }
            if panel_blank || anim.asleep() || guest || link.quiet() {
                link.side.menu.set_open(false);
                link.side.options.set_open(false);
            } else {
                link.side.menu.observe(visible_app, now);
                opening_app = link.side.menu.waiting();
                app_reaction = link.side.menu.take_reaction();
            }
            let options = &link.side.options;
            if options.open() {
                if let Some(font) = text::font() {
                    let style = (options.style_key(), prefs);
                    if options_painted != Some(style) {
                        let picture = options.picture(font, prefs);
                        unsafe {
                            if let Some(old) = options_tex.take() {
                                renderer.delete(&gl, old);
                            }
                            options_tex = Some(renderer.texture(&gl, picture.w as i32, picture.h as i32, &picture.rgba)?);
                        }
                        options_painted = Some(style);
                    }
                    options_visible = true;
                }
            }
            let menu = &link.side.menu;
            if menu.open() {
                if let Some(font) = text::font() {
                    let style = (menu.style_key(true), prefs.menu_labels);
                    if menu_painted != Some(style) {
                        let picture = menu.picture(font, true, prefs.menu_labels);
                        unsafe {
                            if let Some(old) = menu_tex.take() {
                                renderer.delete(&gl, old);
                            }
                            menu_tex = Some(renderer.texture(&gl, picture.w as i32, picture.h as i32, &picture.rgba)?);
                        }
                        menu_painted = Some(style);
                    }
                    menu_position = Some(menu.position);
                }
            }
        }
        let wants_text = menu_position.is_some();
        if wants_text != text_input.is_active() {
            if wants_text {
                text_input.start();
            } else {
                text_input.stop();
            }
        }
        let surface_open = shown.is_some() || menu_position.is_some() || options_visible;
        panel_amount = panel::advance_transition(panel_amount, surface_open, dt);
        let chrome_focus = menu_position.is_some() || options_visible;
        panel_focus = !chrome_focus && shown.as_ref().is_some_and(|h| h.has(panel::flag::FOCUS)) && panel_amount > 0.5;
        let focus = shown.as_ref().filter(|_| !chrome_focus).and_then(|h| {
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
        });
        anim.set_focus(if opening_app {
            Some(anim::Focus { kind: anim::FocusKind::Watching, at: [0.0, -0.66] })
        } else {
            focus
        });
        let job = shown.as_ref()
            .filter(|h| panel_focus && visible_app == Some("terminal") && !h.has(panel::flag::SCROLLED))
            .map(|h| (h.pid, h.job_result()));
        let job_reaction = match (previous_job, job) {
            (Some((old_pid, None)), Some((pid, result))) if old_pid == pid => result,
            _ => None,
        };
        previous_job = job;
        if let Some(success) = app_reaction.or(job_reaction) {
            anim.screen_outcome(success);
        }
        // Once the panel has faded out, its texture goes too.
        if shown.is_none() && panel_amount == 0.0 {
            if let Some(old) = panel_tex.take() {
                unsafe { renderer.delete(&gl, old) };
            }
            lookup_texture = false;
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
                displayed_idle = None;
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
        // The saved Captions setting only collects words; what clears the
        // screen for them is a caption actually being shown.
        let showing_captions = caption_on_screen(link.as_ref().map(|link| &link.side.captions), now);
        if panel_blank || guest || !showing_captions {
            if let Some(old) = caption_tex.take() {
                unsafe { renderer.delete(&gl, old) };
            }
            caption_style = None;
        } else if let (Some(font), Some(link)) = (text::font(), &mut link) {
            let zoom = (h as f32 / 800.0).clamp(1.0, 1.5);
            let columns = ((((w as f32 / zoom) as usize).saturating_sub(84)) / font.w).clamp(24, 96);
            let style = (columns, prefs.caption_heard, prefs.caption_cost);
            if link.side.captions.take_captions_changed() || caption_style != Some(style) {
                if let Some(old) = caption_tex.take() {
                    unsafe { renderer.delete(&gl, old) };
                }
                if let Some(picture) = link.side.captions.picture(font, columns, prefs.caption_heard, prefs.caption_cost) {
                    caption_tex = Some(unsafe {
                        renderer.texture(&gl, picture.w as i32, picture.h as i32, &picture.rgba)?
                    });
                }
                caption_style = Some(style);
            }
        }
        if !panel_blank {
            // Shift+F1 keeps the hardware diagnostics available independently.
            if health_open {
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
                        let lines = link.side.captions.health_lines(&health, columns);
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
            // The level bar, for a second and a half after a change; then,
            // while the speaker is muted, a fainter lasting note.
            let level_line = link.as_ref().and_then(|l| l.side.level.line(now));
            let level_changed = link.as_mut().is_some_and(|l| l.side.level.take_changed());
            level_alpha = if link.as_ref().is_some_and(|l| l.side.level.only_muted_note(now)) { 0.6 } else { 1.0 };
            match (level_line, text::font()) {
                (Some(line), Some(font))
                    if level_changed || level_tex.is_none() || level_shown.as_deref() != Some(line.as_str()) =>
                {
                    let columns = line.chars().count();
                    let pic = font.render(std::slice::from_ref(&line), columns, text::Ink { rgb: CAPTION_INK, backing: 200 });
                    unsafe {
                        if let Some(old) = level_tex.take() {
                            renderer.delete(&gl, old);
                        }
                        level_tex = Some(renderer.texture(&gl, pic.w as i32, pic.h as i32, &pic.rgba)?);
                    }
                    level_shown = Some(line);
                }
                (Some(_), Some(_)) => {}
                _ => {
                    if let Some(old) = level_tex.take() {
                        unsafe { renderer.delete(&gl, old) };
                    }
                    level_shown = None;
                }
            }
            // A software renderer says so on screen for its first half
            // minute, too dim for anyone's eye detector to count.
            if software && !offline_scene && scene_time < SOFTWARE_NOTE.as_secs_f64() {
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
        let notice = link.as_ref().and_then(|link| link.night_notice(now)).filter(|_| {
            !panel_blank && !anim.asleep() && panel_amount == 0.0 && !privacy.guest && !showing_captions && !health_open
                && level_tex.is_none()
        });
        if notice != night_notice.as_ref().map(|(stages, _)| *stages) {
            if let Some((_, old)) = night_notice.take() {
                unsafe { renderer.delete(&gl, old) };
            }
            if let (Some(stages), Some(font)) = (notice, text::font()) {
                let units = if stages == 1 { "stage" } else { "stages" };
                let line = format!("Night shift · {stages} {units} finished · Journal");
                let columns = line.chars().count();
                let pic = font.render(&[line], columns, text::Ink { rgb: CAPTION_INK, backing: 200 });
                night_notice = Some((stages, unsafe { renderer.texture(&gl, pic.w as i32, pic.h as i32, &pic.rgba)? }));
            }
        }
        let layer_space = panel_amount == 0.0 && shown.is_none() && !chrome_focus
            && !showing_captions && !health_open && level_tex.is_none() && night_notice.is_none();
        let use_prefs = !args.override_layers;
        let layers = decor::Layers {
            brows: args.preview_brows || (use_prefs && prefs.brows),
            mouth: args.preview_mouth || (use_prefs && prefs.mouth),
            details: args.preview_details || (use_prefs && prefs.eye_details),
        };
        let mut motion = if layer_space { anim.decor_motion(offline_scene) } else { None };
        if let (Some(motion), Some(speech)) = (&mut motion, &args.speech) {
            let start = if args.audition.is_some() { AUDITION_START } else { 0.45 };
            motion.viseme = speech.sample(clock - start);
        }
        let mood_colour = args.mood_colour || (use_prefs && prefs.mood_colour);
        if mood_colour {
            prefs.palette(&mut look);
        }
        if let Some(palette) = args.palette {
            palette.apply(&mut look);
        }
        if mood_colour {
            // The tint mixes onto the owner's palette. While an overlay hides
            // the layers it eases back to neutral (the palette) rather than
            // snapping, so overlays coming and going never flip the colour.
            let (tint, strength) = motion.map_or((ExprId::Neutral.visual().tint, 1.0), |motion| {
                (mood_tint(motion.expression, motion.blend), motion.strength)
            });
            mood.update(tint, strength, dt);
            mood.apply(&mut look);
        }
        decor.update(layers, motion, dt);
        look.decor = decor;
        let eyes = prefs.eyes(pose.uniforms());
        let show_effects = effects_enabled || (use_prefs && prefs.effects);
        look.accent_emotes = accent_emotes(show_effects, mood_colour);
        let effects = if show_effects && layer_space { anim.emote_motion(offline_scene) } else { None };
        look.glitch = effects.map_or([0.0; 4], |(effects, _, age)| emote::glitch(effects[emote::GLITCH], age));
        look.emotes = effects.map_or_else(emote::Sprites::default, |(effects, burst, age)| {
            emote::sprites(&effects, burst, &eyes, age, seed)
        });
        // A failed compile is logged once and the eyes draw without it.
        if decor.active() || look.glitch[3] > 0.0 {
            let _ = unsafe { renderer.enable_decor(&gl) };
        }
        if look.emotes.len() > 0 {
            let _ = unsafe { renderer.enable_emotes(&gl) };
        }
        let widget_now = Instant::now();
        let widget_link = link.as_ref().filter(|_| !guest && !privacy.guest);
        let widget_readings = widget_link.and_then(|link| link.widget_readings(widget_now));
        let widget_weather = widget_link.filter(|_| prefs.weather_widget && !anim.asleep())
            .and_then(|link| link.online_weather(widget_now));
        let widgets = widget_readings.is_some() || widget_weather.is_some();
        unsafe { widget_tiles.refresh(&gl, &renderer, widget_readings.unwrap_or_default(),
                                      prefs.clock_widget, prefs.system_widget, widget_weather)? };
        let scene = Scene {
            widgets: widgets.then_some(&widget_tiles),
            blank: panel_blank,
            amount: panel_amount,
            eyes,
            look: &look,
            prefs,
            panel: panel_tex.as_ref(),
            menu: menu_position.zip(menu_tex.as_ref()),
            options: options_tex.as_ref().filter(|_| options_visible),
            overlays: &overlays,
            caption: caption_tex.as_ref().filter(|_| !health_open),
            night: night_notice.as_ref().map(|(_, texture)| texture),
            level: level_tex.as_ref(),
            level_alpha,
        };

        if let Some(dir) = &args.frames {
            let (fw, fh) = args.size;
            let px = unsafe { render::capture(&gl, fw, fh, || scene.draw(&gl, &mut renderer, fw as i32, fh as i32, 1))? };
            write_png(&dir.join(format!("frame-{frame_no:05}.png")), fw, fh, &px)?;
        }
        frame_no += 1;

        // Compare to the frame actually on scanout. A sequence of small pose
        // changes still crosses the threshold and presents a new frame.
        let pending_panel_wake = link.as_ref().is_some_and(link::Link::pending_panel_wake);
        let idle_frame = if skip_idle_swaps && !surface_open && !pending_panel_wake && anim.trial_idle() {
            scene.settled_eyes(w, h)
        } else {
            None
        };
        let idle_unchanged = idle_frame.as_ref().zip(displayed_idle.as_ref())
            .is_some_and(|(next, prior)| next.same_image(prior));
        let mut drew = false;
        let mut drawing = Duration::ZERO;
        let mut swapped_at = None;
        if !hidden && !(panel_blank && blank_presented) && !idle_unchanged {
            unsafe { scene.draw(&gl, &mut renderer, w, h, scale)? };
            drawing = frame_start.elapsed();
            window.gl_swap_window();
            swapped_at = Some(Instant::now());
            drew = true;
            displayed_idle = idle_frame;
            if !first_presented {
                first_presented = true;
                eprintln!("face: first frame presented {:.3} s after process start", process_started.elapsed().as_secs_f64());
            }
            if !first_visible && !panel_blank {
                first_visible = true;
                eprintln!("face: first visible eye frame {:.3} s after process start", process_started.elapsed().as_secs_f64());
            }
            blank_presented = panel_blank;
            if let Some(link) = &mut link {
                link.presented();
            }
        }
        drop(overlays);
        if let Some(link) = &mut link {
            link.report_look(prefs);
            if panel_blank {
                // Nothing heard is kept while the screen is dark.
                link.side.captions.forget();
            }
        }

        // Draw only as often as what is on screen needs (task 2.1).
        let activity = anim.activity();
        rate = pace::hz(activity, software);
        if panel_amount > 0.0 && panel_amount < 1.0 {
            rate = pace::hz(pace::Activity::Active, software);
        } else if surface_open {
            rate = rate.max(pace::hz(pace::Activity::Calm, software));
        } else if idle_unchanged {
            rate = pace::hz(pace::Activity::Blank, software);
        } else if let Some(idle) = idle_trial.filter(|_| !showing_captions && !health_open && level_tex.is_none() && anim.trial_idle()) {
            rate = rate.min(idle);
        }
        if look.decor.moving() || look.emotes.len() > 0 || look.glitch[3] > 0.0 {
            rate = rate.max(pace::hz(pace::Activity::Calm, software));
        }
        let work = frame_start.elapsed();
        if drew {
            frame_ms = if frame_ms == 0.0 { work.as_secs_f64() * 1e3 } else { 0.9 * frame_ms + 0.1 * work.as_secs_f64() * 1e3 };
        }
        stats.frame(work, drawing, drew, rate);
        stats.presentation(swapped_at.filter(|_| activity == pace::Activity::Active), idle_unchanged);
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

    watchdog.stopping();
    if let Some(probe) = pointer_probe {
        eprintln!("{}", probe.summary(video.current_video_driver()));
    }
    if power.restore() {
        if let Some(fd) = dpms::sdl_drm_fd(&window) {
            let _ = connectors.set(fd, dpms::DPMS_ON);
        }
    }
    if let Some(path) = &args.record {
        let (w, h) = args.size;
        let overlays: Vec<(&render::Texture, i32)> = overlay_tex.iter().map(|t| (t, 8)).collect();
        let now = Instant::now();
        let link_widgets = link.as_ref().filter(|link| !link.side.menu.guest() && !watch.get().guest);
        let widget_readings = link_widgets.and_then(|link| link.widget_readings(now));
        let widget_weather = link_widgets.filter(|_| prefs.weather_widget && !anim.asleep())
            .and_then(|link| link.online_weather(now));
        unsafe { widget_tiles.refresh(&gl, &renderer, widget_readings.unwrap_or_default(),
                                      prefs.clock_widget, prefs.system_widget, widget_weather)? };
        let scene = Scene {
            widgets: (widget_readings.is_some() || widget_weather.is_some()).then_some(&widget_tiles),
            blank: anim.panel_blank(),
            amount: panel_amount,
            eyes: prefs.eyes(pose.uniforms()),
            look: &look,
            prefs,
            panel: panel_tex.as_ref(),
            menu: link.as_ref().filter(|link| link.side.menu.open()).map(|link| link.side.menu.position).zip(menu_tex.as_ref()),
            options: options_tex.as_ref().filter(|_| link.as_ref().is_some_and(|link| link.side.options.open())),
            overlays: &overlays,
            caption: caption_tex.as_ref().filter(|_| !health_open),
            night: None,
            level: level_tex.as_ref(),
            level_alpha,
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
    widgets: Option<&'a widgets::Tiles>,
    blank: bool,
    amount: f32,
    eyes: [f32; 32],
    look: &'a render::Look,
    prefs: look::Prefs,
    panel: Option<&'a render::Texture>,
    menu: Option<(quick_menu::Position, &'a render::Texture)>,
    options: Option<&'a render::Texture>,
    /// Each overlay with its margin from the bottom-left corner.
    overlays: &'a [(&'a render::Texture, i32)],
    caption: Option<&'a render::Texture>,
    night: Option<&'a render::Texture>,
    /// The level bar, centred low on the screen at twice the text size.
    level: Option<&'a render::Texture>,
    level_alpha: f32,
}

struct SettledEyes {
    rect: panel::Rect,
    output: (i32, i32),
    eyes: [f32; 32],
    colours: [[f32; 3]; 3],
    grid: f32,
    glow_radius: f32,
    marks: render::Marks,
}

impl SettledEyes {
    fn same_image(&self, other: &Self) -> bool {
        let edge_tolerance = 0.5 / self.rect.h.max(1) as f32;
        self.rect == other.rect && self.output == other.output && self.colours == other.colours
            && self.grid == other.grid && self.glow_radius == other.glow_radius && self.marks == other.marks
            && self.eyes.iter().zip(&other.eyes).enumerate().all(|(channel, (&a, &b))| {
                let tolerance = if channel % 16 < 4 { edge_tolerance } else { 0.0001 };
                (a - b).abs() <= tolerance
            })
    }
}

impl Scene<'_> {
    fn settled_eyes(&self, w: i32, h: i32) -> Option<SettledEyes> {
        if self.blank || self.amount != 0.0 || self.widgets.is_some() || self.panel.is_some()
            || self.menu.is_some() || self.options.is_some() || !self.overlays.is_empty()
            || self.caption.is_some() || self.night.is_some() || self.level.is_some()
            || self.look.decor.active() || self.look.emotes.len() != 0 || self.look.glitch[3] > 0.0
        {
            return None;
        }
        let screen = panel::Rect { x: 0, y: 0, w, h };
        Some(SettledEyes {
            rect: self.prefs.eye_rect(screen, w, h, 0.0),
            output: (w, h),
            eyes: self.eyes,
            colours: [self.look.fill, self.look.core, self.look.halo],
            grid: self.look.grid,
            glow_radius: self.look.glow_r,
            marks: self.look.marks,
        })
    }

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
            let eyes = self.prefs.eye_rect(layout.eyes, w, h, self.amount);
            renderer.draw_scaled_in(gl, eyes, scale, &self.eyes, self.look)?;
            if let Some(tex) = self.options {
                let rect = options::Options::rect(layout.panel, tex.w, tex.h);
                renderer.draw_texture(gl, tex, rect, w, h, layout.panel_alpha);
            } else if let Some((position, tex)) = self.menu {
                let rect = position.rect(layout.panel, tex.w, tex.h);
                renderer.draw_texture(gl, tex, rect, w, h, layout.panel_alpha);
            } else if let Some(tex) = self.panel {
                renderer.draw_texture(gl, tex, layout.panel, w, h, layout.panel_alpha);
            }
        } else {
            let screen = panel::Rect { x: 0, y: 0, w, h };
            let eyes = self.prefs.eye_rect(screen, w, h, 0.0);
            if eyes == screen {
                renderer.draw_scaled(gl, w, h, scale, &self.eyes, self.look)?;
            } else {
                render::blank(gl);
                renderer.draw_scaled_in(gl, eyes, scale, &self.eyes, self.look)?;
            }
            if let Some(widgets) = self.widgets {
                widgets.draw(gl, renderer, w, h, self.prefs.widget_positions());
            }
        }
        let zoom = (h / 800).max(1);
        for &(tex, margin) in self.overlays {
            let rect = panel::Rect { x: margin * zoom, y: margin * zoom, w: tex.w * zoom, h: tex.h * zoom };
            renderer.draw_texture(gl, tex, rect, w, h, 1.0);
        }
        if let Some(tex) = self.level {
            renderer.draw_texture(gl, tex, level_rect(tex.w, tex.h, w, h), w, h, self.level_alpha);
        } else if let Some(tex) = self.night {
            renderer.draw_texture(gl, tex, level_rect(tex.w, tex.h, w, h), w, h, 1.0);
        }
        if let Some(tex) = self.caption {
            let zoom = (h as f32 / 800.0).clamp(1.0, 1.5)
                .min((w - 16).max(1) as f32 / tex.w as f32)
                .min((h / 3).max(1) as f32 / tex.h as f32);
            let (width, height) = ((tex.w as f32 * zoom).round() as i32, (tex.h as f32 * zoom).round() as i32);
            let bottom = self.level.map_or((h / 14).max(8), |level| {
                let rect = level_rect(level.w, level.h, w, h);
                rect.y + rect.h + 10
            });
            let rect = panel::Rect { x: (w - width) / 2, y: bottom.min((h - height).max(0)), w: width, h: height };
            renderer.draw_texture(gl, tex, rect, w, h, 1.0);
        }
        Ok(())
    }
}

/// Where the level bar goes on a `w` by `h` screen: centred, a sixth of the
/// way up, at twice the overlay text's size (never wider than the screen).
fn level_rect(tex_w: i32, tex_h: i32, w: i32, h: i32) -> panel::Rect {
    let mut zoom = 2 * (h / 800).max(1);
    while zoom > 1 && tex_w * zoom > w {
        zoom -= 1;
    }
    let (bw, bh) = (tex_w * zoom, tex_h * zoom);
    panel::Rect { x: ((w - bw) / 2).max(0), y: h / 6, w: bw, h: bh }
}

/// Keys that ask for attention while the eyes rest (`key.attend`): the
/// keyboard's way of saying "Hey Pip" when it cannot hear you.
const ATTEND_KEYS: [Keycode; 3] = [Keycode::Space, Keycode::Return, Keycode::KpEnter];

/// A press (or a click, which has no key) asks for attention only while the
/// eyes rest and no app panel has the keyboard: typing into the terminal
/// stays typing.
/// A helper command started from the frame loop without waiting for it:
/// a slow PID 1 (at boot, or with the model on every core) must not freeze
/// the eyes or miss the 5 s watchdog. One at a time; reaped on later frames.
#[derive(Default)]
struct Launcher {
    child: Option<std::process::Child>,
}

impl Launcher {
    /// Start `program` unless the last one is still running. True if started.
    fn start(&mut self, program: &str, args: &[&str]) -> bool {
        self.reap();
        if self.child.is_some() {
            return false;
        }
        match std::process::Command::new(program)
            .args(args)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .spawn()
        {
            Ok(child) => {
                self.child = Some(child);
                true
            }
            Err(e) => {
                eprintln!("face: could not run {program}: {e}");
                false
            }
        }
    }

    /// Collect a finished command, never waiting.
    fn reap(&mut self) {
        if let Some(child) = &mut self.child {
            match child.try_wait() {
                Ok(Some(status)) => {
                    if !status.success() {
                        eprintln!("face: the setup start command failed: {status}");
                    }
                    self.child = None;
                }
                Ok(None) => {}
                Err(_) => self.child = None,
            }
        }
    }

    #[cfg(test)]
    fn running(&self) -> bool {
        self.child.is_some()
    }
}

fn asks_attention(anim: &anim::Anim, panel_focus: bool) -> bool {
    !panel_focus && anim.resting()
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
/// being-console.c) waits before it takes the screen. Esc, Enter, Space, F2
/// or Mission Control brings it up at once. `being.verbose_hold=SECONDS` changes the wait, and
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

    /// `kick` runs every tenth of a second while waiting, so the service
    /// watchdog does not take a long verbose wait for a frozen face.
    #[cfg(target_os = "linux")]
    pub fn hold_for_boot_messages(who: &str, kick: &mut dyn FnMut()) {
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
                "being-{who}: verbose boot: the screen stays on these messages for {} s; press Esc or Enter to go on now",
                d.as_secs()
            ),
            None => format!("being-{who}: verbose boot: the screen stays on these messages; press Esc or Enter to go on"),
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
        // Esc, Enter, Space, F2, keypad Enter, and Mission Control (the
        // laptop's printed F2 is brightness up: hid_apple.fnmode=1).
        const KEYS: [u16; 6] = [1, 28, 57, 60, 96, crate::keys::KEY_SCALE];
        let mut devices: Vec<(PathBuf, File)> = Vec::new();
        let mut scanned: Option<Instant> = None;
        let started = Instant::now();
        let mut buf = vec![0u8; size * 64];
        let pressed = 'wait: loop {
            kick();
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
const AUDITION_START: f64 = 2.4;
const AUDITION_GESTURE: f64 = 3.35;
const AUDITION_HOLD: f64 = 2.7;
const AUDITION_END: f64 = 6.3;

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

/// Accent glyph colours (pink, gold, red, green) belong to the opt-in mood
/// colour layer; with effects alone the glyphs stay in the eyes' own colours.
/// Whether a caption is on screen now. The saved Captions setting alone only
/// collects words, so it must not hide the layers, the night notice or idle pacing.
fn caption_on_screen(captions: Option<&overlay::Captions>, now: std::time::Instant) -> bool {
    captions.is_some_and(|captions| captions.displaying(now))
}

fn accent_emotes(effects: bool, mood_colour: bool) -> bool {
    effects && mood_colour
}

fn mood_tint(expression: ExprId, blend: Option<(ExprId, f32)>) -> eye::MoodTint {
    let primary = expression.visual().tint;
    let Some((other, weight)) = blend else { return primary };
    let other = other.visual().tint;
    eye::MoodTint {
        amount: primary.amount + (other.amount - primary.amount) * weight,
        colour: std::array::from_fn(|i| primary.colour[i] + (other.colour[i] - primary.colour[i]) * weight),
    }
}

/// Render still images and exit: `--snapshot` and `--sheet`.
fn stills(
    gl: &glow::Context,
    renderer: &render::Renderer,
    look: &render::Look,
    args: &Args,
    effects_enabled: bool,
) -> Result<(), String> {
    let capture_expression = |name: &str, w, h| {
        let expression = ExprId::from_name(name).ok_or_else(|| format!("no expression called {name}"))?;
        let pose = if args.source_pose { expression.concept_pose() } else { expression.pose() };
        let eyes = pose.uniforms();
        let mut look = *look;
        if let Some(palette) = args.palette {
            palette.apply(&mut look);
        }
        if args.mood_colour {
            colour::Mood::still(expression.visual().tint, 1.0).apply(&mut look);
        }
        let layers = decor::Layers { brows: args.preview_brows, mouth: args.preview_mouth, details: args.preview_details };
        // Freeze the effects at the same point in their motion on every still.
        let phase = 0.60;
        look.decor = decor::Decor::still(layers, expression, phase);
        if effects_enabled {
            let mut effects = expression.visual().effects;
            for (value, on) in effects.iter_mut().zip(args.manual_emotes) {
                if on { *value = value.max(1.0); }
            }
            look.glitch = emote::glitch(effects[emote::GLITCH], phase);
            look.emotes = emote::sprites(&effects, 0.0, &eyes, phase, args.seed.unwrap_or(1));
        }
        unsafe { renderer.snapshot(gl, w, h, &eyes, &look) }
    };
    if let Some(path) = &args.snapshot {
        let (w, h) = args.size;
        let px = capture_expression(&args.expr, w, h)?;
        write_png(path, w, h, &px)?;
        println!("wrote {} ({}, {w}x{h})", path.display(), args.expr);
    }
    if let Some(path) = &args.sheet {
        let names: Vec<&str> = if args.names.is_empty() {
            eye::SHEET.to_vec()
        } else if args.names == ["all"] {
            ExprId::ALL.iter().map(|id| id.name()).collect()
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
            let tile = capture_expression(name, tw, th)?;
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
    use super::{anim, asks_attention, level_rect, Launcher, ATTEND_KEYS};
    use sdl2::keyboard::Keycode;
    use std::time::Duration;

    #[test]
    fn accent_glyph_colours_need_the_mood_colour_layer() {
        assert!(!super::accent_emotes(true, false), "effects alone stay in the eyes' colours");
        assert!(super::accent_emotes(true, true));
        assert!(!super::accent_emotes(false, true));
    }

    #[test]
    fn the_saved_captions_setting_alone_leaves_room_for_the_layers() {
        // layer_space, the night notice and idle pacing all key off a caption
        // being shown, not off the setting that collects words.
        let mut captions = super::overlay::Captions::default();
        captions.set_enabled(true);
        let now = std::time::Instant::now();
        assert!(captions.on);
        assert!(!super::caption_on_screen(Some(&captions), now), "the setting alone shows nothing");
        assert!(!super::caption_on_screen(None, now));
    }

    #[test]
    fn audition_arguments_select_a_bounded_native_scene() {
        let parse = |args: &[&str]| super::parse_args(args.iter().map(|value| value.to_string()));
        let args = parse(&["--audition", "wink", "--intensity", "0.65", "--gesture", "nod", "--frames", "poses"]).unwrap();
        assert_eq!(args.audition, Some(super::ExprId::Wink));
        assert_eq!(args.intensity, 0.65);
        assert_eq!(args.gesture.as_deref(), Some("nod"));
        assert_eq!(args.exit_after, Some(super::AUDITION_END));
        assert!(parse(&["--audition", "typo"]).is_err());
        assert!(parse(&["--audition", "wink", "--intensity", "NaN"]).is_err());
        assert!(parse(&["--audition", "wink", "--demo"]).is_err());
        assert!(parse(&["--gesture", "shake"]).is_err());
        let animated = parse(&["--audition", "starstruck", "--gesture", "eye-roll", "--full"]).unwrap();
        assert!(animated.preview_brows && animated.preview_mouth && animated.preview_details && animated.effects);
        let scene = parse(&["--script", "heartbreak", "--frames", "poses"]).unwrap();
        assert_eq!(scene.exit_after, super::audition::duration("heartbreak"));
        assert!(parse(&["--script", "heartbreak", "--exit-after", "inf"]).is_err());
    }

    #[test]
    fn pointer_probe_requires_bounded_standalone_fullscreen_run() {
        let parse = |args: &[&str]| super::parse_args(args.iter().map(|v| v.to_string()));
        for (seconds, accepted) in [("0", false), ("60", true), ("61", false)] {
            let args = ["--pointer-probe", "--fullscreen", "--standalone", "--exit-after", seconds];
            assert_eq!(parse(&args).is_ok(), accepted);
        }
        assert!(parse(&["--pointer-probe", "--fullscreen", "--exit-after", "5"]).is_err());
        assert!(parse(&["--pointer-probe", "--fullscreen", "--standalone"]).is_err());
        assert!(parse(&["--pointer-probe", "--fullscreen", "--standalone", "--hidden", "--exit-after", "5"]).is_err());
    }

    #[test]
    fn the_level_bar_sits_centred_low_and_fits_the_screen() {
        let r = level_rect(308, 28, 1280, 800);
        assert_eq!((r.w, r.h), (616, 56), "twice the text size on the laptop panel");
        assert_eq!((r.x, r.y), ((1280 - 616) / 2, 800 / 6));
        let big = level_rect(308, 28, 2560, 1600);
        assert_eq!(big.w, 308 * 4);
        let narrow = level_rect(308, 28, 400, 800);
        assert!(narrow.w <= 400 && narrow.x >= 0, "{}", narrow.w);
    }

    #[test]
    fn the_setup_key_starts_its_command_without_waiting_for_it() {
        // A PID 1 slow to answer: the frame loop must not wait.
        let mut l = Launcher::default();
        let began = std::time::Instant::now();
        assert!(l.start("sleep", &["2"]));
        assert!(began.elapsed() < Duration::from_millis(500), "the frame loop waited {:?}", began.elapsed());
        assert!(l.running());
        assert!(!l.start("sleep", &["2"]), "a second press does not stack another");
        l.reap();
        assert!(l.running(), "reaping never waits");
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while l.running() {
            assert!(std::time::Instant::now() < deadline, "never reaped");
            std::thread::sleep(Duration::from_millis(50));
            l.reap();
        }
        assert!(!l.start("/nonexistent/systemctl", &[]), "a missing command is only logged");
    }

    #[test]
    fn space_return_and_a_click_ask_for_attention_only_while_resting_and_unfocused() {
        assert!(ATTEND_KEYS.contains(&Keycode::Space) && ATTEND_KEYS.contains(&Keycode::Return));
        assert!(!ATTEND_KEYS.contains(&Keycode::M) && !ATTEND_KEYS.contains(&Keycode::Escape));
        let mut a = anim::Anim::new(5);
        a.set_lid(Some(false), false, true);
        a.set_state("idle", false, false);
        assert!(asks_attention(&a, false));
        assert!(!asks_attention(&a, true), "a focused terminal gets the space");
        a.set_state("listening", false, false);
        assert!(!asks_attention(&a, false), "already listening");
        a.set_state("asleep", false, false);
        assert!(asks_attention(&a, false), "a key wakes sleeping eyes, as its name does");
        a.set_lid(Some(true), false, false);
        assert!(!asks_attention(&a, false), "the lid is the privacy switch");
    }

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
