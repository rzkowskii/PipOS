//! The emotion director: the pure core.
//!
//! Everything here is a function of its arguments, the embedded table and a
//! small RAM-only tracker. No clocks (time arrives as an `f64` of seconds), no
//! I/O, no model calls, no regex crate. The table in
//! `data/emotion-director.json` is the single source of truth for poses; the
//! mind only knows family ids. See `docs/design/emotion-director.md` (v2).
//!
//! Evidence labels in comments: source reading, host test, estimate.
//!
//! # Wiring contract
//!
//! The functions here are pure and do not know the machine, so the wiring
//! step in `Machine` carries these rules (pinned by the `wiring_contract_*`
//! tests):
//!
//! 1. **Gate plus Dwell for every output.** Every `face.express` the director
//!    produces, whatever its source (heard cue, reply clause, event row, job
//!    row), goes through `emit`: `gate` (the body, guest, announcing and
//!    quiet vetoes), the phase rule below, then `Dwell::try_show` (min gap,
//!    per-turn cap, startle spacing). A refusal at any step shows nothing and
//!    records nothing. `wire_choice` and `choose` do not gate by themselves
//!    for a wire cue; `emit` is what makes them safe.
//! 2. **Replies only on an accepted turn, while Thinking or Speaking.** A
//!    heard cue shows only while Thinking; reply-clause cues only while
//!    Thinking (the first-clause lead) or Speaking; never while Listening or
//!    Idle, and never without an accepted turn (`Moment::accepted_turn`: the
//!    mind took the turn and it is not a follow-up it ignored). Spontaneous
//!    and job outputs have no phase rule beyond the gate.
//! 3. **One `choose` call per moment.** The heard moment uses `choose_heard`
//!    (never a tag, never reply words); each reply clause uses `choose_reply`
//!    (never a heard cue).
//! 4. **Emit only what adds.** For a reply clause the wiring emits the choice
//!    only if `adds_to_tag` says it differs from the clause's model tag or
//!    adds intensity above the face's own speech cue (`FACE_CUE_INTENSITY`).
//!
//! What lives here:
//! * `Table`: load and validation of the embedded JSON (no runtime fallback: a
//!   table that fails to validate makes the director inert, and a unit test
//!   asserts that the embedded one validates);
//! * `read_clause`: Pip's own reply clause to a `Cue` (social families only);
//! * `choose` (and `choose_heard`, `choose_reply`): the blend of cues from
//!   all sources into one `Choice`, one call per moment;
//! * `gate`, `admit` and `emit`: the quiet, guest and body veto of design
//!   section 5, the phase rule, and the one door every output goes through;
//! * `Dwell`: the pacing rules (min gap, per-turn cap, startle spacing);
//! * `reachability`: for each of the 89 names, group, family, rung, live or waiting.

use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

const TABLE_JSON: &str = include_str!("../data/emotion-director.json");

/// The scan buffers are fixed arrays; the table's caps must fit them.
const SCAN_BYTES_LIMIT: usize = 512;
const SCAN_WORDS_LIMIT: usize = 64;

// ---------------------------------------------------------------------------
// Families, cues, sources
// ---------------------------------------------------------------------------

/// The ten families of the shared contract. Social and conversational only:
/// there is no family for a sense or a body state, so no text can raise one.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Family {
    Warm,
    Humour,
    Curious,
    Doubt,
    Worry,
    Surprise,
    Thanks,
    Apology,
    Support,
    Calm,
}

impl Family {
    pub const ALL: [Family; 10] = [
        Family::Warm,
        Family::Humour,
        Family::Curious,
        Family::Doubt,
        Family::Worry,
        Family::Surprise,
        Family::Thanks,
        Family::Apology,
        Family::Support,
        Family::Calm,
    ];

    pub fn id(self) -> &'static str {
        match self {
            Family::Warm => "warm",
            Family::Humour => "humour",
            Family::Curious => "curious",
            Family::Doubt => "doubt",
            Family::Worry => "worry",
            Family::Surprise => "surprise",
            Family::Thanks => "thanks",
            Family::Apology => "apology",
            Family::Support => "support",
            Family::Calm => "calm",
        }
    }

    pub fn from_id(id: &str) -> Option<Family> {
        Family::ALL.iter().copied().find(|f| f.id() == id)
    }

    fn idx(self) -> usize {
        self as usize
    }
}

/// A feeling: a family and a strength 0..1.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Cue {
    pub family: Family,
    pub strength: f64,
}

impl Cue {
    /// Strength is clamped to 0..1; a non-finite strength becomes 0.
    pub fn new(family: Family, strength: f64) -> Cue {
        let strength = if strength.is_finite() { strength.clamp(0.0, 1.0) } else { 0.0 };
        Cue { family, strength }
    }

    /// The wire form of `mind.emotion {family, strength}`. Unknown family or a
    /// non-finite strength gives `None` (the event is then ignored).
    pub fn from_wire(family: &str, strength: f64) -> Option<Cue> {
        if !strength.is_finite() {
            return None;
        }
        Some(Cue::new(Family::from_id(family)?, strength))
    }
}

/// Where a cue came from. The weights live in the table.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Source {
    /// The person's words, read by the mind after it accepted the turn.
    Heard,
    /// Pip's own reply words (`read_clause`).
    Reply,
    /// An event row: think timeout, camera look, missed hearing.
    Event,
    /// A terminal job row: failed or finished.
    Job,
    /// The model's face tag (`tag_cue`).
    Tag,
    /// A small drive nudge (arousal, social, tiredness).
    Drive,
}

impl Source {
    fn idx(self) -> usize {
        self as usize
    }

    /// What kind of output a cue from this source would produce. Drives only
    /// nudge and are not gated by themselves.
    fn output(self) -> Option<Output> {
        match self {
            Source::Heard => Some(Output::Word),
            Source::Reply | Source::Tag => Some(Output::Reply),
            Source::Event => Some(Output::Spontaneous),
            Source::Job => Some(Output::Job),
            Source::Drive => None,
        }
    }
}

/// The intensity at which the face shows a model tag as its own speech cue
/// (source: anim.rs `speech_level`). A director choice that only repeats the
/// tag, at or below this, adds nothing.
pub const FACE_CUE_INTENSITY: f64 = 0.55;

const SOURCE_NAMES: [&str; 6] = ["heard", "reply", "event", "job", "tag", "drive"];

/// What the director decided to show.
#[derive(Clone, Debug, PartialEq)]
pub struct Choice<'t> {
    pub name: &'t str,
    pub intensity: f64,
    pub hold: f64,
    pub family: Family,
    pub rung: usize,
    /// The source that led the winning family (it sets the hold).
    pub source: Source,
}

// ---------------------------------------------------------------------------
// State flags and the gate (design section 5)
// ---------------------------------------------------------------------------

/// The state flags the veto needs. All false is a plain awake day.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Ctx {
    pub asleep: bool,
    pub waking: bool,
    pub drowsy: bool,
    pub muted: bool,
    pub unwell: bool,
    pub lid_blocked: bool,
    pub guest: bool,
    /// An announcement (or private line) is being spoken.
    pub announcing: bool,
    /// Night, dark, or an untrusted clock (fails closed).
    pub quiet: bool,
    /// Screen Effects are on (unknown must be passed as false). Carried for
    /// the drawn-mark and stand-in decisions of the face lane; the ladders
    /// themselves hold only eyes-only-safe poses of group a and a*.
    pub effects_on: bool,
}

impl Ctx {
    fn body_veto(&self) -> bool {
        self.asleep || self.waking || self.lid_blocked || self.muted || self.unwell
    }
}

/// What kind of output is being asked for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Output {
    /// A reaction to the person's words.
    Word,
    /// A reaction to Pip's own reply (a direct answer to an accepted turn).
    /// Blocked while a guest is present (see `gate`).
    Reply,
    /// Anything else the director would start by itself.
    Spontaneous,
    /// A terminal job row (failed or finished).
    Job,
}

/// Why the gate said no.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Veto {
    /// Asleep, waking, lid blocked, muted or unwell: the face's own poses own the face.
    Body,
    /// Night, dark, untrusted clock or drowsy, for anything not a reply.
    Quiet,
    /// A guest is present: only job rows. No word reactions and, by design,
    /// no reply-clause reactions either (the guest sees the face's own cues only).
    Guest,
    /// An announcement or private line is being spoken.
    Announcing,
}

/// The state table of design section 5.
///
/// | state or flag | allowed |
/// |---|---|
/// | asleep, waking, lid blocked, muted, unwell | nothing |
/// | drowsy, quiet | word and reply reactions only (nothing spontaneous, no job rows) |
/// | guest | job rows only |
/// | announcing | job rows only |
///
/// Guest mode blocks reply clauses on purpose, not only heard words: a guest
/// is somebody whose presence Pip may not read or react to, and a reaction to
/// Pip's own reply would still be a reaction to a conversation with the
/// guest in the room. The face keeps its own speech cue (intensity 0.55) as
/// the only visible tone. This is the design (section 5, "guest: state poses
/// and job rows only, no word reactions"), read strictly, and a unit test pins
/// it. Relaxing it is a decision for the coordinator, not a bug fix.
pub fn gate(ctx: &Ctx, out: Output) -> Result<(), Veto> {
    if ctx.body_veto() {
        return Err(Veto::Body);
    }
    if out != Output::Job {
        if ctx.announcing {
            return Err(Veto::Announcing);
        }
        if ctx.guest {
            return Err(Veto::Guest);
        }
    }
    if matches!(out, Output::Spontaneous | Output::Job) && (ctx.quiet || ctx.drowsy) {
        return Err(Veto::Quiet);
    }
    Ok(())
}

/// What the machine is doing, as far as the director cares.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Listening,
    Thinking,
    Speaking,
}

/// The conversation moment an output would be shown in.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Moment {
    pub phase: Phase,
    /// The mind accepted the current turn (not a follow-up it ignored, not a
    /// guest, not speech that was not meant for Pip).
    pub accepted_turn: bool,
}

/// Why `admit` or `emit` said no.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    Veto(Veto),
    /// The output does not belong in this phase, or there is no accepted turn.
    Phase,
    /// `Dwell` refused (gap, per-turn cap or startle spacing).
    Pacing,
}

/// The gate plus the phase rule (wiring contract, items 1 and 2).
///
/// | output | allowed when |
/// |---|---|
/// | `Word` | Thinking, on an accepted turn |
/// | `Reply` | Thinking or Speaking, on an accepted turn |
/// | `Spontaneous`, `Job` | always (the gate decides) |
pub fn admit(ctx: &Ctx, out: Output, at: Moment) -> Result<(), Refusal> {
    gate(ctx, out).map_err(Refusal::Veto)?;
    let fits = match out {
        Output::Word => at.accepted_turn && at.phase == Phase::Thinking,
        Output::Reply => at.accepted_turn && matches!(at.phase, Phase::Thinking | Phase::Speaking),
        Output::Spontaneous | Output::Job => true,
    };
    if fits {
        Ok(())
    } else {
        Err(Refusal::Phase)
    }
}

/// `admit`, then `Dwell::try_show`: the one door every director output goes
/// through. On `Ok` the pose has been recorded in `dwell`; on any refusal
/// nothing is recorded.
pub fn emit(ctx: &Ctx, at: Moment, out: Output, name: &str, dwell: &mut Dwell, now: f64) -> Result<(), Refusal> {
    admit(ctx, out, at)?;
    if dwell.try_show(name, now) {
        Ok(())
    } else {
        Err(Refusal::Pacing)
    }
}

// ---------------------------------------------------------------------------
// The table
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct Rung {
    pose: String,
    min_strength: f64,
}

#[derive(Clone, Debug)]
struct Ladder {
    polarity: i8,
    min_intensity: f64,
    /// The lowest strength a heard (`mind.emotion`) cue of this family needs
    /// (surprise 0.3: a faint "surprise" must not select a startle pose).
    wire_min: f64,
    rungs: Vec<Rung>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Word,
    Phrase,
    Start,
    Whole,
}

/// One phrase: a word per slot, each slot with one or more alternatives.
type Slots = Vec<Vec<String>>;

#[derive(Clone, Debug)]
struct Row {
    rule: String,
    kind: Kind,
    phrases: Vec<Slots>,
    family: Family,
    strength: f64,
    /// Words must be separated by spaces only (a comma between them breaks the phrase).
    tight: bool,
    /// The match must be followed by the end of the clause or one of `.,!?;:`
    /// (so a word after it cannot turn the phrase into something else).
    after_break: bool,
    /// The polarity of the row's family (-1, 0, 1).
    polarity: i8,
    /// A weak greeting: dropped when any other row matches the clause.
    opener: bool,
    /// Phrases that must not follow the match directly (`thanks to`).
    refuse_next: Vec<Slots>,
    /// At most this many words may follow the match.
    max_rest: Option<usize>,
    /// A positive or neutral row refuses a clause with a caution word or a
    /// negator outside the match (default true; the comforting support rows,
    /// which answer bad news, set false).
    cautions: bool,
}

#[derive(Clone, Debug)]
struct DwellCfg {
    min_gap: f64,
    max_per_turn: u32,
    kick_gap: f64,
    kick_poses: Vec<String>,
}

#[derive(Debug)]
pub struct Table {
    weights: [f64; 6],
    holds: [f64; 6],
    min_score: f64,
    intensity_min: f64,
    intensity_max: f64,
    wire_hold: (f64, f64),
    dwell: DwellCfg,
    afterglow_intensity: f64,
    afterglow_hold: f64,
    scan_words: usize,
    scan_bytes: usize,
    /// words that split a clause into two opinions (`but`, `however` ...)
    contrast: Vec<Vec<u8>>,
    /// words of trouble or loss: a positive or neutral reading is refused beside them
    cautions: HashSet<Vec<u8>>,
    tags: Vec<(String, Option<Cue>)>,
    ladders: Vec<Ladder>,
    lexicon: Vec<Row>,
    /// first word (every alternative) to the (row, phrase) pairs that begin with it
    index: HashMap<Vec<u8>, Vec<(usize, usize)>>,
    never_emit: Vec<String>,
    groups: Vec<(String, Vec<String>)>,
}

/// The embedded table, parsed and validated once. `None` means the embedded
/// JSON is invalid: the director is then inert (there is no fallback table);
/// a unit test asserts it is `Some`.
pub fn table() -> Option<&'static Table> {
    static TABLE: OnceLock<Option<Table>> = OnceLock::new();
    TABLE.get_or_init(|| Table::parse(TABLE_JSON).ok()).as_ref()
}

fn field<'a>(v: &'a Value, key: &str) -> Result<&'a Value, String> {
    v.get(key).ok_or_else(|| format!("missing field {key}"))
}

fn num(v: &Value, key: &str) -> Result<f64, String> {
    let n = field(v, key)?.as_f64().ok_or_else(|| format!("{key} is not a number"))?;
    if n.is_finite() {
        Ok(n)
    } else {
        Err(format!("{key} is not finite"))
    }
}

fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str, String> {
    field(v, key)?.as_str().ok_or_else(|| format!("{key} is not a string"))
}

fn list<'a>(v: &'a Value, key: &str) -> Result<&'a Vec<Value>, String> {
    field(v, key)?.as_array().ok_or_else(|| format!("{key} is not a list"))
}

fn strings(v: &Value, key: &str) -> Result<Vec<String>, String> {
    list(v, key)?
        .iter()
        .map(|s| s.as_str().map(str::to_owned).ok_or_else(|| format!("{key} holds a non-string")))
        .collect()
}

fn unit(n: f64, what: &str) -> Result<f64, String> {
    if (0.0..=1.0).contains(&n) {
        Ok(n)
    } else {
        Err(format!("{what} is outside 0..1"))
    }
}

/// Words are lowercase ASCII letters, digits and inner apostrophes.
fn is_word_text(s: &str) -> bool {
    !s.is_empty()
        && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'\'')
        && !s.starts_with('\'')
        && !s.ends_with('\'')
}

fn parse_phrase(src: &str) -> Result<Slots, String> {
    let mut slots = Vec::new();
    for word in src.split(' ') {
        let alts: Vec<String> = word.split('|').map(str::to_owned).collect();
        if alts.iter().any(|a| !is_word_text(a)) {
            return Err(format!("bad lexicon word in {src:?}"));
        }
        slots.push(alts);
    }
    Ok(slots)
}

impl Table {
    /// Parse and validate. Every check here is also what the unit tests rely on.
    pub fn parse(json: &str) -> Result<Table, String> {
        let root: Value = serde_json::from_str(json).map_err(|e| format!("table json: {e}"))?;

        // weights and holds, in `Source` order
        let w = field(&root, "weights")?;
        let h = field(&root, "holds")?;
        let mut weights = [0.0; 6];
        let mut holds = [0.0; 6];
        for (i, name) in SOURCE_NAMES.iter().enumerate() {
            weights[i] = num(w, name)?;
            holds[i] = num(h, name)?;
            if weights[i] <= 0.0 || holds[i] <= 0.0 {
                return Err(format!("weight and hold of {name} must be positive"));
            }
        }

        let blend = field(&root, "blend")?;
        let min_score = num(blend, "min_score")?;
        let intensity_min = unit(num(blend, "intensity_min")?, "intensity_min")?;
        let intensity_max = unit(num(blend, "intensity_max")?, "intensity_max")?;
        if intensity_min > intensity_max || min_score <= 0.0 {
            return Err("blend range is inconsistent".into());
        }
        // a drive alone must never reach the threshold
        if weights[Source::Drive.idx()] >= min_score {
            return Err("a drive alone would reach the threshold".into());
        }

        let wire = field(&root, "wire")?;
        let wire_hold = (num(wire, "hold_min")?, num(wire, "hold_max")?);
        if wire_hold.0 <= 0.0 || wire_hold.0 > wire_hold.1 {
            return Err("wire hold range is inconsistent".into());
        }

        let d = field(&root, "dwell")?;
        let dwell = DwellCfg {
            min_gap: num(d, "min_gap")?,
            max_per_turn: num(d, "max_per_turn")? as u32,
            kick_gap: num(d, "kick_gap")?,
            kick_poses: strings(d, "kick_poses")?,
        };
        if dwell.min_gap < 0.0 || dwell.kick_gap < 0.0 || dwell.max_per_turn == 0 {
            return Err("dwell constants are inconsistent".into());
        }

        let ag = field(&root, "afterglow")?;
        let afterglow_intensity = unit(num(ag, "intensity")?, "afterglow intensity")?;
        let afterglow_hold = num(ag, "hold")?;
        if afterglow_hold <= 0.0 {
            return Err("afterglow hold must be positive".into());
        }

        let sc = field(&root, "scan")?;
        let scan_words = num(sc, "max_words")? as usize;
        let scan_bytes = num(sc, "max_bytes")? as usize;
        if scan_words == 0 || scan_words > SCAN_WORDS_LIMIT || scan_bytes == 0 || scan_bytes > SCAN_BYTES_LIMIT {
            return Err("scan caps outside the fixed buffers".into());
        }

        // groups and the blocklist
        let g = field(&root, "groups")?
            .as_object()
            .ok_or("groups is not an object")?;
        let mut groups: Vec<(String, Vec<String>)> = Vec::new();
        for (k, v) in g {
            let names = v
                .as_array()
                .ok_or("a group is not a list")?
                .iter()
                .map(|s| s.as_str().map(str::to_owned).ok_or("a group holds a non-string"))
                .collect::<Result<Vec<_>, _>>()?;
            groups.push((k.clone(), names));
        }
        let group_of = |name: &str| -> Option<&str> {
            groups.iter().find(|(_, ns)| ns.iter().any(|n| n == name)).map(|(k, _)| k.as_str())
        };
        {
            let mut seen: Vec<&str> = Vec::new();
            for (_, ns) in &groups {
                for n in ns {
                    if seen.contains(&n.as_str()) {
                        return Err(format!("{n} is in two groups"));
                    }
                    seen.push(n);
                }
            }
        }
        let mut never_emit: Vec<String> = Vec::new();
        let ne = field(&root, "never_emit")?;
        if ne.as_object().map(|o| o.len()) != Some(3) {
            return Err("never_emit must list exactly b, c1 and c0".into());
        }
        for g in ["b", "c1", "c0"] {
            let mut listed = strings(ne, g)?;
            let mut expect: Vec<String> =
                groups.iter().filter(|(k, _)| k == g).flat_map(|(_, ns)| ns.iter().cloned()).collect();
            listed.sort();
            expect.sort();
            if listed != expect {
                return Err(format!("never_emit {g} differs from group {g}"));
            }
            never_emit.extend(listed);
        }

        // families and ladders
        let mut ladders: Vec<Option<Ladder>> = vec![None; Family::ALL.len()];
        for f in list(&root, "families")? {
            let id = text(f, "id")?;
            let family = Family::from_id(id).ok_or_else(|| format!("unknown family {id}"))?;
            if ladders[family.idx()].is_some() {
                return Err(format!("family {id} listed twice"));
            }
            let polarity = num(f, "polarity")? as i8;
            if !(-1..=1).contains(&polarity) {
                return Err(format!("polarity of {id} out of range"));
            }
            let min_intensity = unit(num(f, "min_intensity")?, "min_intensity")?;
            let wire_min = match f.get("wire_min") {
                None => 0.0,
                Some(_) => unit(num(f, "wire_min")?, "wire_min")?,
            };
            let mut rungs = Vec::new();
            for r in list(f, "rungs")? {
                let pose = text(r, "pose")?.to_owned();
                let min_strength = unit(num(r, "min_strength")?, "min_strength")?;
                match group_of(&pose) {
                    Some("a") | Some("a*") => {}
                    other => return Err(format!("{id}: pose {pose} is in group {other:?}, not a or a*")),
                }
                if never_emit.contains(&pose) {
                    return Err(format!("{id}: pose {pose} is on the blocklist"));
                }
                rungs.push(Rung { pose, min_strength });
            }
            if rungs.is_empty() || rungs[0].min_strength != 0.0 {
                return Err(format!("{id}: rung 0 must exist and start at strength 0"));
            }
            if rungs.windows(2).any(|w| w[1].min_strength <= w[0].min_strength) {
                return Err(format!("{id}: rung strengths must increase"));
            }
            ladders[family.idx()] = Some(Ladder { polarity, min_intensity, wire_min, rungs });
        }
        let ladders: Vec<Ladder> = ladders
            .into_iter()
            .zip(Family::ALL)
            .map(|(l, f)| l.ok_or_else(|| format!("family {} has no ladder", f.id())))
            .collect::<Result<_, _>>()?;
        // the double blink: surprise-family anchors need intensity >= 0.55
        if ladders[Family::Surprise.idx()].min_intensity < 0.55 {
            return Err("surprise family needs min_intensity >= 0.55".into());
        }
        for k in &dwell.kick_poses {
            if group_of(k).is_none() {
                return Err(format!("kick pose {k} is not a known name"));
            }
        }

        // contrast and caution words
        let contrast: Vec<Vec<u8>> = strings(&root, "contrast")?.into_iter().map(String::into_bytes).collect();
        let cautions: HashSet<Vec<u8>> = strings(&root, "cautions")?.into_iter().map(String::into_bytes).collect();
        for w in contrast.iter().chain(cautions.iter()) {
            if !std::str::from_utf8(w).is_ok_and(is_word_text) {
                return Err("contrast and caution entries must be single lowercase words".into());
            }
        }
        if contrast.is_empty() || cautions.is_empty() {
            return Err("contrast and cautions must not be empty".into());
        }

        // tags: all seven present; neutral and sleepy count nothing
        let mut tags = Vec::new();
        let tag_obj = field(&root, "tags")?.as_object().ok_or("tags is not an object")?;
        for tag in ["neutral", "joy", "curiosity", "surprise", "skepticism", "concern", "sleepy"] {
            let v = tag_obj.get(tag).ok_or_else(|| format!("tag {tag} missing"))?;
            let cue = if v.is_null() {
                None
            } else {
                let fam = Family::from_id(text(v, "family")?).ok_or("tag names an unknown family")?;
                Some(Cue::new(fam, unit(num(v, "strength")?, "tag strength")?))
            };
            if matches!(tag, "neutral" | "sleepy") && cue.is_some() {
                return Err(format!("tag {tag} must count nothing"));
            }
            tags.push((tag.to_owned(), cue));
        }
        if tag_obj.len() != 7 {
            return Err("tags must be exactly the seven model tags".into());
        }

        // lexicon
        let mut lexicon: Vec<Row> = Vec::new();
        for r in list(&root, "lexicon")? {
            let rule = text(r, "rule")?.to_owned();
            if lexicon.iter().any(|x| x.rule == rule) {
                return Err(format!("rule {rule} listed twice"));
            }
            let kind = match text(r, "kind")? {
                "word" => Kind::Word,
                "phrase" => Kind::Phrase,
                "start" => Kind::Start,
                "whole" => Kind::Whole,
                other => return Err(format!("{rule}: unknown kind {other}")),
            };
            let family = Family::from_id(text(r, "family")?).ok_or_else(|| format!("{rule}: unknown family"))?;
            let strength = unit(num(r, "strength")?, "lexicon strength")?;
            if strength <= 0.0 {
                return Err(format!("{rule}: strength must be above 0"));
            }
            let tight = match r.get("tight") {
                None => false,
                Some(v) => v.as_bool().ok_or_else(|| format!("{rule}: tight must be true or false"))?,
            };
            let after_break = match r.get("after") {
                None => false,
                Some(v) if v.as_str() == Some("break") => true,
                Some(_) => return Err(format!("{rule}: after can only be \"break\"")),
            };
            let opener = match r.get("opener") {
                None => false,
                Some(v) => v.as_bool().ok_or_else(|| format!("{rule}: opener must be true or false"))?,
            };
            let cautions = match r.get("cautions") {
                None => true,
                Some(v) => v.as_bool().ok_or_else(|| format!("{rule}: cautions must be true or false"))?,
            };
            let max_rest = match r.get("max_rest") {
                None => None,
                Some(_) => Some(num(r, "max_rest")? as usize),
            };
            let mut refuse_next = Vec::new();
            if r.get("refuse_next").is_some() {
                for p in strings(r, "refuse_next")? {
                    refuse_next.push(parse_phrase(&p).map_err(|e| format!("{rule}: {e}"))?);
                }
            }
            if opener && kind != Kind::Start {
                return Err(format!("{rule}: an opener must be a start row"));
            }
            let mut phrases = Vec::new();
            for p in strings(r, "any")? {
                let slots = parse_phrase(&p).map_err(|e| format!("{rule}: {e}"))?;
                if kind == Kind::Word && slots.len() != 1 {
                    return Err(format!("{rule}: a word row takes single words"));
                }
                if slots.len() > scan_words {
                    return Err(format!("{rule}: phrase longer than the scan"));
                }
                phrases.push(slots);
            }
            if phrases.is_empty() {
                return Err(format!("{rule}: no patterns"));
            }
            let fam_ladder = &ladders[family.idx()];
            lexicon.push(Row {
                rule,
                kind,
                phrases,
                family,
                strength,
                tight,
                after_break,
                polarity: fam_ladder.polarity,
                opener,
                refuse_next,
                max_rest,
                cautions,
            });
        }

        let mut index: HashMap<Vec<u8>, Vec<(usize, usize)>> = HashMap::new();
        for (ri, row) in lexicon.iter().enumerate() {
            for (pi, slots) in row.phrases.iter().enumerate() {
                for alt in &slots[0] {
                    index.entry(alt.as_bytes().to_vec()).or_default().push((ri, pi));
                }
            }
        }

        Ok(Table {
            weights,
            holds,
            min_score,
            intensity_min,
            intensity_max,
            wire_hold,
            dwell,
            afterglow_intensity,
            afterglow_hold,
            scan_words,
            scan_bytes,
            contrast,
            cautions,
            tags,
            ladders,
            lexicon,
            index,
            never_emit,
            groups,
        })
    }

    /// Poses the director must never emit: groups b, c1 and c0.
    pub fn never_emit(&self) -> &[String] {
        &self.never_emit
    }

    pub fn is_blocked(&self, name: &str) -> bool {
        self.never_emit.iter().any(|n| n == name)
    }

    /// The ladder of a family as pose names, rung 0 first.
    pub fn ladder(&self, family: Family) -> Vec<&str> {
        self.ladders[family.idx()].rungs.iter().map(|r| r.pose.as_str()).collect()
    }

    pub fn polarity(&self, family: Family) -> i8 {
        self.ladders[family.idx()].polarity
    }

    /// The lowest strength a heard cue of the family needs (0 for most,
    /// 0.3 for surprise). Applied by `wire_choice` and by `choose` for heard votes.
    pub fn wire_min(&self, family: Family) -> f64 {
        self.ladders[family.idx()].wire_min
    }

    pub fn weight(&self, source: Source) -> f64 {
        self.weights[source.idx()]
    }

    pub fn lexicon_len(&self) -> usize {
        self.lexicon.len()
    }

    /// The group of a pose name per the embedded copy of the sheet.
    pub fn group_of(&self, name: &str) -> Option<&str> {
        self.groups.iter().find(|(_, ns)| ns.iter().any(|n| n == name)).map(|(k, _)| k.as_str())
    }

    // -- strength to rung and intensity ------------------------------------

    /// Strength 0..1 to (rung, intensity 0.4..1.0). Mild feeling is the same
    /// anchor at lower intensity; surprise-family anchors never go below 0.55.
    pub fn map_strength(&self, family: Family, strength: f64) -> (usize, f64) {
        let ladder = &self.ladders[family.idx()];
        let s = if strength.is_finite() { strength.clamp(0.0, 1.0) } else { 0.0 };
        let rung = ladder.rungs.iter().rposition(|r| r.min_strength <= s + 1e-9).unwrap_or(0);
        let raw = self.intensity_min + (self.intensity_max - self.intensity_min) * s;
        let intensity = raw.clamp(self.intensity_min, self.intensity_max).max(ladder.min_intensity);
        (rung, intensity)
    }

    fn choice(&self, family: Family, strength: f64, source: Source, hold: f64) -> Choice<'_> {
        let (rung, intensity) = self.map_strength(family, strength);
        Choice {
            name: &self.ladders[family.idx()].rungs[rung].pose,
            intensity,
            hold,
            family,
            rung,
            source,
        }
    }

    /// The mind's `mind.emotion {family, strength, hold?}` mapped through the
    /// ladder. A heard cue must reach the same threshold as in `choose`
    /// (heard weight x strength >= `min_score`), so a near-zero strength shows
    /// nothing. The hold is clamped to the wire range; an unknown family gives
    /// `None`.
    ///
    /// **The caller must gate.** This applies neither `gate(ctx, Output::Word)`
    /// nor `Dwell`: the wiring step has to run both on every wire cue (a guest,
    /// a body veto, the announcing flag, the pacing rules), or a wire cue
    /// would slip past the quiet, guest and body rules that `choose` enforces.
    pub fn wire_choice(&self, family: &str, strength: f64, hold: Option<f64>) -> Option<Choice<'_>> {
        let cue = Cue::from_wire(family, strength)?;
        if cue.strength <= 0.0 || self.weights[Source::Heard.idx()] * cue.strength < self.min_score {
            return None;
        }
        // a per-family floor: a faint surprise must not select a startle pose
        if cue.strength + 1e-9 < self.wire_min(cue.family) {
            return None;
        }
        let hold = match hold {
            Some(h) if h.is_finite() => h.clamp(self.wire_hold.0, self.wire_hold.1),
            _ => self.holds[Source::Heard.idx()].clamp(self.wire_hold.0, self.wire_hold.1),
        };
        Some(self.choice(cue.family, cue.strength, Source::Heard, hold))
    }

    /// The model tag as a cue. `neutral` counts 0 and `sleepy` (a group-b
    /// state pose) is ignored: both give `None`. Unknown tags give `None`.
    pub fn tag_cue(&self, tag: &str) -> Option<Cue> {
        self.tags.iter().find(|(t, _)| t.eq_ignore_ascii_case(tag)).and_then(|(_, c)| *c)
    }

    /// The low-intensity closing pose after a completed, uninterrupted reply.
    /// Startle-kick poses give `None` (a glow must not kick again).
    pub fn afterglow<'a>(&'a self, c: &Choice<'a>) -> Option<Choice<'a>> {
        if self.dwell.kick_poses.iter().any(|k| k == c.name) {
            return None;
        }
        let floor = self.ladders[c.family.idx()].min_intensity;
        Some(Choice {
            name: c.name,
            intensity: self.afterglow_intensity.max(floor),
            hold: self.afterglow_hold,
            family: c.family,
            rung: c.rung,
            source: c.source,
        })
    }

    // -- the blend ---------------------------------------------------------

    /// Blend cues from all sources into one choice, or `None`.
    ///
    /// **Call it once per moment, never across moments.** The heard moment
    /// (`choose_heard`: the person's words, at `Thinking`) and each reply
    /// clause (`choose_reply`: the clause's words plus the model tag) are
    /// separate calls with separate votes. Mixing them makes a joy tag on the
    /// reply wipe out a sad reaction to what was just said (the tag-versus-
    /// negative-text rule below drops both). `choose` itself is the shared
    /// blend, and it does not know the phase: the wiring runs `admit` and
    /// `Dwell` on whatever it returns (see "Wiring contract" at the top).
    ///
    /// * each vote is gated by its source (`gate`); drives are not gated;
    /// * a heard vote below its family's `wire_min` counts nothing;
    /// * a choice led by the model tag alone is capped at `FACE_CUE_INTENSITY`
    ///   (the face already shows the tag at that level), and a lone surprise
    ///   tag gives `None` (every rung of that ladder is a startle pose);
    /// * a `Tag` vote of the calm family counts 0 (the neutral tag);
    /// * a positive tag is never flipped to a negative family by text alone,
    ///   and never shown against it either: when a positive-family tag vote
    ///   meets negative-family heard or reply words, both are dropped (so a
    ///   lone tag and such text show nothing; jobs, events and other text
    ///   still count);
    /// * score per family is the sum of weight x strength over real sources;
    ///   a family needs `min_score` to compete, so a drive alone can never
    ///   fire; drives add their weighted strength to the comparison and to the
    ///   result as a small nudge; ties go to the family with the heavier
    ///   leading source, then to table order;
    /// * the strength of the result is the real score divided by the heaviest
    ///   weight in the family (one vote keeps its own strength, agreeing
    ///   sources raise it) plus the drive nudge, capped at 1.
    pub fn choose(&self, votes: &[(Source, Cue)], ctx: &Ctx) -> Option<Choice<'_>> {
        let mut live: Vec<(Source, Cue)> = Vec::with_capacity(votes.len());
        for &(src, cue) in votes {
            if !cue.strength.is_finite() || cue.strength <= 0.0 {
                continue;
            }
            if src == Source::Tag && cue.family == Family::Calm {
                continue; // neutral tag counts 0
            }
            if src == Source::Heard && cue.strength + 1e-9 < self.wire_min(cue.family) {
                continue; // too faint for this family (surprise)
            }
            if let Some(out) = src.output() {
                if gate(ctx, out).is_err() {
                    continue;
                }
            } else if ctx.body_veto() {
                continue; // a drive never speaks for a body that is vetoed
            }
            live.push((src, Cue::new(cue.family, cue.strength)));
        }

        // A positive tag overruled by negative words shows nothing at all: the
        // tag would otherwise win alone and put a pleased face over sorrowful
        // text, and a wrong reaction is worse than none. Both the tag and the
        // negative text are dropped; real events (jobs) and other text stay.
        let positive_tag = live
            .iter()
            .any(|(s, c)| *s == Source::Tag && self.polarity(c.family) > 0);
        let negative_text =
            |s: &Source, c: &Cue| matches!(s, Source::Heard | Source::Reply) && self.polarity(c.family) < 0;
        if positive_tag && live.iter().any(|(s, c)| negative_text(s, c)) {
            live.retain(|(s, c)| !negative_text(s, c) && !(*s == Source::Tag && self.polarity(c.family) > 0));
        }

        // per family: `score` is weight x strength over real sources, `nudge`
        // is the same over drives (kept apart so a drive can never carry a choice)
        let mut score = [0.0f64; 10];
        let mut nudge = [0.0f64; 10];
        let mut lead_w = [0.0f64; 10]; // heaviest real weight present in the family
        let mut lead_src = [Source::Drive; 10];
        let mut lead_c = [0.0f64; 10]; // contribution of the leading vote
        for (src, cue) in &live {
            let i = cue.family.idx();
            let w = self.weights[src.idx()];
            let contribution = w * cue.strength;
            if *src == Source::Drive {
                nudge[i] += contribution;
                continue;
            }
            score[i] += contribution;
            if w > lead_w[i] + 1e-12 || ((w - lead_w[i]).abs() <= 1e-12 && contribution > lead_c[i]) {
                lead_w[i] = w;
                lead_src[i] = *src;
                lead_c[i] = contribution;
            }
        }

        // only families with real support compete; the drive nudge counts in the comparison
        let mut best: Option<usize> = None;
        for i in 0..Family::ALL.len() {
            if score[i] < self.min_score {
                continue;
            }
            best = match best {
                None => Some(i),
                Some(b) => {
                    let (ti, tb) = (score[i] + nudge[i], score[b] + nudge[b]);
                    let better = ti > tb + 1e-9 || ((ti - tb).abs() <= 1e-9 && lead_w[i] > lead_w[b] + 1e-12);
                    Some(if better { i } else { b })
                }
            };
        }
        let b = best?;
        let family = Family::ALL[b];
        let tag_led = lead_src[b] == Source::Tag;
        if tag_led && family == Family::Surprise {
            return None; // a lone surprise tag never selects a startle rung
        }
        let strength = (score[b] / lead_w[b] + nudge[b]).clamp(0.0, 1.0);
        let mut choice = self.choice(family, strength, lead_src[b], self.holds[lead_src[b].idx()]);
        if tag_led {
            choice.intensity = choice.intensity.min(FACE_CUE_INTENSITY);
        }
        Some(choice)
    }

    /// The heard moment: the cue the mind sent for the person's words (plus
    /// drive nudges). No tag and no reply words are ever passed here; the
    /// reply clauses get their own `choose_reply` calls later.
    pub fn choose_heard(&self, heard: Cue, drives: &[Cue], ctx: &Ctx) -> Option<Choice<'_>> {
        let mut votes: Vec<(Source, Cue)> = Vec::with_capacity(1 + drives.len());
        votes.push((Source::Heard, heard));
        votes.extend(drives.iter().map(|d| (Source::Drive, *d)));
        self.choose(&votes, ctx)
    }

    /// One reply clause: the cue read from the clause's own words and the
    /// model's tag for it (either may be `None`), plus drive nudges. Never
    /// carries a heard cue.
    pub fn choose_reply(&self, clause: Option<Cue>, tag: Option<Cue>, drives: &[Cue], ctx: &Ctx) -> Option<Choice<'_>> {
        let mut votes: Vec<(Source, Cue)> = Vec::with_capacity(2 + drives.len());
        if let Some(c) = clause {
            votes.push((Source::Reply, c));
        }
        if let Some(t) = tag {
            votes.push((Source::Tag, t));
        }
        votes.extend(drives.iter().map(|d| (Source::Drive, *d)));
        self.choose(&votes, ctx)
    }

    /// Should the wiring emit `choice` as a `face.express`, given the model
    /// tag the voice is about to echo for the same clause? Only when it
    /// differs from the tag's family or adds intensity above the face's own
    /// speech cue; otherwise the face already shows it and the director adds
    /// no event.
    pub fn adds_to_tag(&self, choice: &Choice, tag: Option<Cue>) -> bool {
        match tag {
            None => true,
            Some(t) if t.family == Family::Calm => true,
            Some(t) => t.family != choice.family || choice.intensity > FACE_CUE_INTENSITY + 1e-9,
        }
    }

    // -- Pip's own clause --------------------------------------------------

    /// Read one reply clause of Pip's own. The caller skips private,
    /// announcement and camera clauses. Scans at most `max_words` words and
    /// `max_bytes` bytes; ASCII case-insensitive; a clause that opens with a
    /// quotation mark is quoted speech and reads as nothing. Only social
    /// families are reachable, so "I see a bird", "I can hear rain", "I feel
    /// hot" or "I'm tired" read as nothing.
    ///
    /// **All matches are collected, then the clause is judged as a whole**
    /// (a wrong reaction is worse than none):
    /// * a contrast word (`but`, `however`, `though` ...) anywhere gives
    ///   `None`: the clause holds two opinions;
    /// * a weak greeting (`opener`) is dropped when any other row matched, so
    ///   it can never beat the content of the clause (`hello, unfortunately
    ///   that is late` reads as worry);
    /// * two matches of different polarity (positive, neutral, negative) give
    ///   `None`, and so does a positive match that caution words took back
    ///   (`Seen::Suppressed`) beside a negative one;
    /// * otherwise the longest pattern wins, then the stronger one, then the
    ///   earlier row.
    ///
    /// Per match, `row_hit` applies the context guards (negators, hedges,
    /// questions, conditionals, cautions, hyphenated compounds).
    pub fn read_clause(&self, text: &str) -> Option<Cue> {
        self.read_clause_rule(text).map(|(c, _)| c)
    }

    /// Like `read_clause`, also naming the lexicon rule that fired.
    pub fn read_clause_rule(&self, text: &str) -> Option<(Cue, &str)> {
        let mut buf = [0u8; SCAN_BYTES_LIMIT];
        let mut toks = [(0u16, 0u16); SCAN_WORDS_LIMIT];
        let norm = self.normalise(text, &mut buf, &mut toks)?;
        // every word is looked up in the first-word index; only the rows that
        // begin with it are checked (so the cost follows the clause, not the table)
        let mut hits = [(0usize, 0usize, false); MAX_HITS]; // (row, words, suppressed)
        let mut count = 0usize;
        for at in 0..norm.toks.len() {
            let Some(cands) = self.index.get(norm.word(at)) else { continue };
            for &(ri, pi) in cands {
                let row = &self.lexicon[ri];
                let slots = &row.phrases[pi];
                let suppressed = match self.row_hit(row, slots, &norm, at) {
                    Seen::No => continue,
                    Seen::Yes => false,
                    Seen::Suppressed => true,
                };
                if count == MAX_HITS {
                    return None; // a clause that matches this much is noise
                }
                hits[count] = (ri, slots.len(), suppressed);
                count += 1;
            }
        }
        if count == 0 {
            return None;
        }
        let hits = &hits[..count];
        if self.has_contrast(&norm) {
            return None;
        }
        // a weak greeting never competes with content (a suppressed match is
        // still content: it was there, and trouble words took it back)
        let content = hits.iter().any(|&(ri, _, _)| !self.lexicon[ri].opener);
        let mut best: Option<(usize, f64, usize)> = None; // (words, strength, row)
        let mut polarity: Option<i8> = None;
        for &(ri, words, suppressed) in hits {
            let row = &self.lexicon[ri];
            if content && row.opener {
                continue;
            }
            match polarity {
                None => polarity = Some(row.polarity),
                Some(p) if p != row.polarity => return None, // mixed feelings
                Some(_) => {}
            }
            if suppressed {
                continue; // counted for the mix, never shown
            }
            let cand = (words, row.strength, ri);
            let better = match best {
                None => true,
                Some((bl, bs, br)) => {
                    cand.0 > bl
                        || (cand.0 == bl && cand.1 > bs + 1e-12)
                        || (cand.0 == bl && (cand.1 - bs).abs() <= 1e-12 && ri < br)
                }
            };
            if better {
                best = Some(cand);
            }
        }
        best.map(|(_, _, ri)| {
            let row = &self.lexicon[ri];
            (Cue::new(row.family, row.strength), row.rule.as_str())
        })
    }

    /// A contrast word anywhere in the scanned clause.
    fn has_contrast(&self, n: &Norm) -> bool {
        (0..n.toks.len()).any(|i| self.contrast.iter().any(|c| c.as_slice() == n.word(i)))
    }

    /// A caution word (trouble, loss, error) anywhere in the scanned clause.
    fn has_caution(&self, n: &Norm) -> bool {
        (0..n.toks.len()).any(|i| self.cautions.contains(n.word(i)))
    }

    /// Does this phrase of a row match with its first word at token `at`, in
    /// a context that allows the reaction?
    ///
    /// `row_matches_at` covers the shape and the local guards. On top of it:
    /// * `refuse_next` phrases right after the match, and `max_rest`;
    /// * word and phrase rows refuse a sentence that opens with a conditional
    ///   (`if`, `would`, `could`, `might`, `whether`, `in case`);
    /// * rows of a family with polarity 0 or above also refuse a question
    ///   (the first break after the match is `?`, or the sentence opens with
    ///   an interrogative or auxiliary and ends with `?`), and a clause that
    ///   carries a caution word or a negator outside the match (unless the row
    ///   opts out with `cautions: false`). What follows a positive opener may
    ///   take it back (`thanks, that is not what I asked`).
    fn row_hit(&self, row: &Row, slots: &Slots, n: &Norm, at: usize) -> Seen {
        if !row_matches_at(row, slots, n, at) {
            return Seen::No;
        }
        let last = at + slots.len() - 1;
        if !row.refuse_next.is_empty()
            && last + 1 < n.toks.len()
            && n.joined(last + 1, true)
            && row.refuse_next.iter().any(|p| n.slots_at(p, last + 1, true))
        {
            return Seen::No;
        }
        if let Some(max) = row.max_rest {
            if n.truncated || n.toks.len() - 1 - last > max {
                return Seen::No;
            }
        }
        let first = n.sentence_start(at);
        if matches!(row.kind, Kind::Word | Kind::Phrase) && n.opens_conditional(first) {
            return Seen::No;
        }
        if row.polarity >= 0 {
            if n.question_at(first, last) {
                return Seen::No;
            }
            if row.cautions && (self.has_caution(n) || n.negated_outside(at, last)) {
                return Seen::Suppressed;
            }
        }
        Seen::Yes
    }

    /// Lowercase the clause into `buf` and cut it into words.
    ///
    /// * Everything is bounded by the byte cap, leading whitespace included.
    /// * A non-ASCII letter or digit is a word character (never a separator),
    ///   so `Haiku` with a diaeresis is one word that no table word equals.
    ///   Non-ASCII whitespace separates; other non-ASCII punctuation becomes
    ///   `_`, which separates and breaks a phrase.
    /// * When the byte cap lands inside a word (or right before `-` or an
    ///   apostrophe that continues it), the cut last word is dropped: a
    ///   fragment such as `wow` of `wowzers` must not read.
    fn normalise<'b>(
        &self,
        text: &str,
        buf: &'b mut [u8; SCAN_BYTES_LIMIT],
        toks: &'b mut [(u16, u16); SCAN_WORDS_LIMIT],
    ) -> Option<Norm<'b>> {
        let mut cut = text.len().min(self.scan_bytes);
        while !text.is_char_boundary(cut) {
            cut -= 1;
        }
        let head = &text[..cut];
        // quoted speech is not Pip's own statement
        let first = head.trim_start().chars().next()?;
        if matches!(first, '"' | '\u{201c}' | '\u{201d}' | '\u{ab}') {
            return None;
        }
        let mut up = [0u64; SCAN_BYTES_LIMIT / 64];
        let mut n = 0usize;
        for ch in head.chars() {
            buf[n] = match ch {
                '\u{2019}' | '\u{2018}' => b'\'',
                '\u{2010}' | '\u{2011}' => b'-',
                c if c.is_ascii_whitespace() => b' ',
                c if c.is_ascii() => {
                    if c.is_ascii_uppercase() {
                        up[n >> 6] |= 1 << (n & 63);
                    }
                    c.to_ascii_lowercase() as u8
                }
                c if c.is_whitespace() => b' ',
                c if c.is_alphanumeric() => NON_ASCII_WORD,
                _ => b'_',
            };
            n += 1;
        }
        // words: letters, digits, and apostrophes between two of them
        let mut count = 0usize;
        let mut i = 0usize;
        let mut truncated = cut < text.len();
        while i < n {
            if !is_word_byte(buf[i]) {
                i += 1;
                continue;
            }
            let start = i;
            while i < n && (is_word_byte(buf[i]) || (buf[i] == b'\'' && i + 1 < n && is_word_byte(buf[i + 1]))) {
                i += 1;
            }
            if count == self.scan_words {
                truncated = true;
                break;
            }
            toks[count] = (start as u16, i as u16);
            count += 1;
        }
        // the byte cap landed inside a word: that last word is a fragment
        if cut < text.len() && count > 0 && n > 0 {
            let last_end = toks[count - 1].1 as usize;
            let word_before = is_word_byte(buf[n - 1]) || buf[n - 1] == b'\'';
            let word_after = text[cut..]
                .chars()
                .next()
                .is_some_and(|c| c.is_alphanumeric() || matches!(c, '\'' | '\u{2019}' | '\u{2018}' | '-' | '\u{2010}' | '\u{2011}'));
            if word_before && word_after && last_end + 1 >= n {
                count -= 1;
            }
        }
        if count == 0 {
            return None;
        }
        Some(Norm { buf: &buf[..n], toks: &toks[..count], truncated, up })
    }
}

/// What one row made of the clause at one position.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Seen {
    /// No match, or a context that makes it no reading at all (hyphenated
    /// compound, question, conditional, negator before it).
    No,
    /// A match that reads.
    Yes,
    /// A positive or neutral match that trouble words in the clause take
    /// back. It does not read, but it still counts as a feeling in the
    /// clause: beside a negative match the clause is mixed and says nothing.
    Suppressed,
}

/// Marker byte for a non-ASCII letter or digit: a word character that no
/// lexicon word (lowercase ASCII, digits, apostrophes) can contain.
const NON_ASCII_WORD: u8 = 0x80;

fn is_word_byte(b: u8) -> bool {
    b.is_ascii_lowercase() || b.is_ascii_digit() || b == NON_ASCII_WORD
}

/// Words that turn a nearby phrase around. Only checked after a match, so the
/// list costs nothing on a clause without one.
const NEGATORS: &[&[u8]] = &[
    b"not", b"no", b"never", b"without", b"any", b"nothing", b"nobody", b"none", b"neither", b"nor", b"hardly",
    b"barely", b"scarcely", b"rarely", b"seldom", b"unable", b"unlikely", b"fail", b"fails", b"failed", b"cannot",
    b"can't", b"cant", b"don't", b"dont", b"doesn't", b"didn't", b"isn't", b"wasn't", b"aren't", b"weren't",
    b"won't", b"wouldn't", b"couldn't", b"shouldn't", b"haven't", b"hasn't", b"hadn't", b"mustn't", b"needn't",
    b"shan't", b"mightn't", b"ain't",
];

/// Words that make what follows hypothetical (`it could be wonderful`). They
/// count like negators in the look-back window.
const HEDGES: &[&[u8]] = &[b"if", b"would", b"could", b"might", b"may", b"should", b"whether", b"unless"];

/// Phrases that turn the word after them around (`far from fantastic`). They
/// count when they end up to `GUARD_GAP` tokens before the match.
const GUARDS: &[&[&[u8]]] = &[
    &[b"far", b"from"],
    &[b"less", b"than"],
    &[b"anything", b"but"],
    &[b"anything", b"other", b"than"],
    &[b"other", b"than"],
    &[b"short", b"of"],
];
const GUARD_GAP: usize = 2;

/// A sentence that opens with one of these is hypothetical (word and phrase rows).
const CONDITIONAL_OPENERS: &[&[u8]] = &[b"if", b"would", b"could", b"might", b"whether"];

/// A sentence that opens with one of these and ends with `?` is a question.
const INTERROGATIVES: &[&[u8]] = &[
    b"do", b"does", b"did", b"is", b"are", b"was", b"were", b"will", b"would", b"can", b"could", b"should",
    b"might", b"may", b"have", b"has", b"had", b"shall", b"what", b"why", b"how", b"which", b"who", b"whom",
    b"when", b"where", b"whether", b"if",
];

/// More matches than this in one clause is noise, not a reaction.
const MAX_HITS: usize = 16;

/// How many tokens before a match a negator can sit and still turn it around.
const NEGATION_WINDOW: usize = 4;

struct Norm<'b> {
    buf: &'b [u8],
    toks: &'b [(u16, u16)],
    truncated: bool,
    /// bit per `buf` byte: the source letter was an ASCII capital
    up: [u64; SCAN_BYTES_LIMIT / 64],
}

impl Norm<'_> {
    fn word(&self, i: usize) -> &[u8] {
        let (s, e) = self.toks[i];
        &self.buf[s as usize..e as usize]
    }

    /// Words only separated by spaces, or by spaces and commas unless `tight`.
    fn joined(&self, i: usize, tight: bool) -> bool {
        let gap = &self.buf[self.toks[i - 1].1 as usize..self.toks[i].0 as usize];
        gap.iter().all(|&b| b == b' ' || (!tight && b == b','))
    }

    fn slots_at(&self, slots: &Slots, at: usize, tight: bool) -> bool {
        if at + slots.len() > self.toks.len() {
            return false;
        }
        for (k, alts) in slots.iter().enumerate() {
            let w = self.word(at + k);
            if !alts.iter().any(|a| a.as_bytes() == w) {
                return false;
            }
            if k > 0 && !self.joined(at + k, tight) {
                return false;
            }
        }
        true
    }

    /// Nothing but spaces before the first word (so no quote or bracket opens it).
    fn opens_clause(&self) -> bool {
        self.buf[..self.toks[0].0 as usize].iter().all(|&b| b == b' ')
    }

    /// After the last word: nothing, or one of . ! ?
    fn closes_clause(&self) -> bool {
        let tail = &self.buf[self.toks[self.toks.len() - 1].1 as usize..];
        let end = tail.iter().rposition(|&b| b != b' ').map_or(0, |p| p + 1);
        let tail = &tail[..end];
        tail.is_empty() || (tail.len() == 1 && matches!(tail[0], b'.' | b'!' | b'?'))
    }

    /// The match from token `first` to token `last` is part of a hyphenated
    /// compound (`hi-res`, `thanks-giving`, `anti-awesome`).
    fn compound(&self, first: usize, last: usize) -> bool {
        let s = self.toks[first].0 as usize;
        let e = self.toks[last].1 as usize;
        (s > 0 && self.buf[s - 1] == b'-') || (e < self.buf.len() && self.buf[e] == b'-')
    }

    /// Token `i` was written in capitals only (`HI`, not `Hi`).
    fn is_caps(&self, i: usize) -> bool {
        let (s, e) = self.toks[i];
        let mut upper = false;
        for k in s as usize..e as usize {
            if self.up[k >> 6] & (1 << (k & 63)) != 0 {
                upper = true;
            } else if self.buf[k].is_ascii_lowercase() {
                return false;
            }
        }
        upper
    }

    /// No sentence-ending punctuation between tokens `from` and `to` (`. ! ? ; :`).
    fn same_sentence(&self, from: usize, to: usize) -> bool {
        (from + 1..=to).all(|j| {
            let gap = &self.buf[self.toks[j - 1].1 as usize..self.toks[j].0 as usize];
            !gap.iter().any(|b| matches!(b, b'.' | b'!' | b'?' | b';' | b':'))
        })
    }

    /// A negator or hedge within `NEGATION_WINDOW` tokens before token `at`,
    /// in the same sentence (`. ! ? ; :` between tokens ends the look-back),
    /// or a guard phrase (`far from`, `less than`) just before it.
    fn negated_before(&self, at: usize) -> bool {
        let mut j = at;
        for _ in 0..NEGATION_WINDOW {
            if j == 0 {
                break;
            }
            let gap = &self.buf[self.toks[j - 1].1 as usize..self.toks[j].0 as usize];
            if gap.iter().any(|b| matches!(b, b'.' | b'!' | b'?' | b';' | b':')) {
                return false;
            }
            j -= 1;
            let w = self.word(j);
            if NEGATORS.contains(&w) || HEDGES.contains(&w) {
                return true;
            }
        }
        self.guarded_before(at)
    }

    /// A guard phrase ends at most `GUARD_GAP` tokens before `at`, in the same sentence.
    fn guarded_before(&self, at: usize) -> bool {
        for g in GUARDS {
            for gap in 0..=GUARD_GAP {
                if at < gap + g.len() {
                    continue;
                }
                let start = at - gap - g.len();
                if (0..g.len()).all(|k| self.word(start + k) == g[k]) && self.same_sentence(start, at) {
                    return true;
                }
            }
        }
        false
    }

    /// A negator anywhere in the clause outside the match (tokens `at` to
    /// `last`). `any` is too common to count here.
    fn negated_outside(&self, at: usize, last: usize) -> bool {
        (0..self.toks.len()).any(|i| {
            (i < at || i > last) && {
                let w = self.word(i);
                // cheap length filter first: the list is 2 to 9 bytes, most words are not
                (2..=9).contains(&w.len()) && w != b"any" && NEGATORS.contains(&w)
            }
        })
    }

    /// The first token of the sentence that holds token `at` (`. ! ?` end a sentence).
    fn sentence_start(&self, at: usize) -> usize {
        let mut j = at;
        while j > 0 {
            let gap = &self.buf[self.toks[j - 1].1 as usize..self.toks[j].0 as usize];
            if gap.iter().any(|b| matches!(b, b'.' | b'!' | b'?')) {
                break;
            }
            j -= 1;
        }
        j
    }

    /// The sentence that starts at token `first` is hypothetical: `if`,
    /// `would`, `could`, `might`, `whether` or `in case` opens it.
    fn opens_conditional(&self, first: usize) -> bool {
        let w = self.word(first);
        CONDITIONAL_OPENERS.contains(&w) || (w == b"in" && first + 1 < self.toks.len() && self.word(first + 1) == b"case")
    }

    /// The first of `. ! ?` after token `last`.
    fn sentence_end_mark(&self, last: usize) -> Option<u8> {
        self.buf[self.toks[last].1 as usize..].iter().copied().find(|b| matches!(b, b'.' | b'!' | b'?'))
    }

    /// The first of `. , ! ? ; :` after token `last`.
    fn first_break_after(&self, last: usize) -> Option<u8> {
        self.buf[self.toks[last].1 as usize..]
            .iter()
            .copied()
            .find(|b| matches!(b, b'.' | b',' | b'!' | b'?' | b';' | b':'))
    }

    /// The match from token `last` back to the sentence start `first` sits in
    /// a question: its sentence ends with `?` and either the first break
    /// after the match is that `?`, or the sentence opens with an
    /// interrogative or auxiliary (`was that a good idea?`, `do you want the
    /// good news, or the bad?`). A sentence cut off by the byte cap that
    /// opens that way counts as a question too.
    fn question_at(&self, first: usize, last: usize) -> bool {
        let opens_question = INTERROGATIVES.contains(&self.word(first));
        match self.sentence_end_mark(last) {
            Some(b'?') => opens_question || self.first_break_after(last) == Some(b'?'),
            None => self.truncated && opens_question,
            Some(_) => false,
        }
    }

    /// After token `last`: the end of the clause, or one of `. , ! ? ; :`.
    fn breaks_after(&self, last: usize) -> bool {
        let rest = &self.buf[self.toks[last].1 as usize..];
        match rest.iter().find(|&&b| b != b' ') {
            None => true,
            Some(b) => matches!(b, b'.' | b',' | b'!' | b'?' | b';' | b':' | b'_'),
        }
    }
}

/// Does this phrase of a row match with its first word at token `at`?
///
/// After the shape of the row (its kind) fits, the context guards apply to
/// every kind: a hyphenated compound never matches; a one or two letter match
/// in capitals is an abbreviation (`HI` for Hawaii); word and phrase rows are
/// refused when a negator or hedge sits within `NEGATION_WINDOW` tokens before
/// them or a guard phrase (`far from`) just before; rows with `after: break`
/// need a clause boundary right after. Context beyond the match (questions,
/// conditionals, cautions, contrast) is `Table::row_hit` and `read_clause_rule`.
fn row_matches_at(row: &Row, slots: &Slots, n: &Norm, at: usize) -> bool {
    let shape = match row.kind {
        Kind::Word | Kind::Phrase => n.slots_at(slots, at, row.tight),
        Kind::Start => at == 0 && n.opens_clause() && n.slots_at(slots, 0, row.tight),
        Kind::Whole => {
            at == 0
                && n.opens_clause()
                && !n.truncated
                && n.toks.len() == slots.len()
                && n.slots_at(slots, 0, row.tight)
                && n.closes_clause()
        }
    };
    if !shape {
        return false;
    }
    let last = at + slots.len() - 1;
    if n.compound(at, last) {
        return false;
    }
    if slots.len() == 1 && n.word(at).len() <= 2 && n.is_caps(at) {
        return false;
    }
    if matches!(row.kind, Kind::Word | Kind::Phrase) && n.negated_before(at) {
        return false;
    }
    !row.after_break || n.breaks_after(last)
}

// ---------------------------------------------------------------------------
// Free functions over the embedded table
// ---------------------------------------------------------------------------

/// Read one clause of Pip's own reply (see `Table::read_clause`).
pub fn read_clause(text: &str) -> Option<Cue> {
    table()?.read_clause(text)
}

/// Blend cues (see `Table::choose`).
pub fn choose(votes: &[(Source, Cue)], ctx: &Ctx) -> Option<Choice<'static>> {
    table()?.choose(votes, ctx)
}

/// The heard moment (see `Table::choose_heard`).
pub fn choose_heard(heard: Cue, drives: &[Cue], ctx: &Ctx) -> Option<Choice<'static>> {
    table()?.choose_heard(heard, drives, ctx)
}

/// One reply clause (see `Table::choose_reply`).
pub fn choose_reply(clause: Option<Cue>, tag: Option<Cue>, drives: &[Cue], ctx: &Ctx) -> Option<Choice<'static>> {
    table()?.choose_reply(clause, tag, drives, ctx)
}

/// Whether a reply choice adds to the clause's model tag (see `Table::adds_to_tag`).
pub fn adds_to_tag(choice: &Choice, tag: Option<Cue>) -> bool {
    table().is_some_and(|t| t.adds_to_tag(choice, tag))
}

/// The model tag as a cue (see `Table::tag_cue`).
pub fn tag_cue(tag: &str) -> Option<Cue> {
    table()?.tag_cue(tag)
}

/// The mind's `mind.emotion {family, strength, hold?}` through the ladder.
pub fn wire_choice(family: &str, strength: f64, hold: Option<f64>) -> Option<Choice<'static>> {
    table()?.wire_choice(family, strength, hold)
}

pub fn afterglow(c: &Choice<'static>) -> Option<Choice<'static>> {
    table()?.afterglow(c)
}

// ---------------------------------------------------------------------------
// Dwell
// ---------------------------------------------------------------------------

/// Pacing of director pose changes. Time is an argument (seconds on any
/// monotonic base), so it replays with fake time.
///
/// * at least `min_gap` (1.2 s) between pose changes;
/// * at most `max_per_turn` (3) changes per turn;
/// * no startle-kick pose (surprise, startled, scared, shocked) twice within
///   `kick_gap` (10 s), the same pose or another;
/// * showing the same pose again is a refresh, not a change.
///
/// A non-finite time is refused. If time runs backwards, the pacing history
/// (gap and count) is ignored, so a clock jump cannot freeze the face; the
/// startle spacing alone fails closed (a time before the last kick counts as
/// inside the gap) until `reset`, so a jump cannot let a startle repeat.
#[derive(Clone, Debug)]
pub struct Dwell {
    cfg: DwellCfg,
    last_at: Option<f64>,
    last_name: Option<String>,
    changes: u32,
    kick_at: Option<f64>,
}

impl Dwell {
    pub fn with_table(t: &Table) -> Dwell {
        Dwell { cfg: t.dwell.clone(), last_at: None, last_name: None, changes: 0, kick_at: None }
    }

    /// From the embedded table; an inert table gives a dwell that refuses everything.
    pub fn new() -> Dwell {
        match table() {
            Some(t) => Dwell::with_table(t),
            None => Dwell {
                cfg: DwellCfg { min_gap: 0.0, max_per_turn: 0, kick_gap: 0.0, kick_poses: Vec::new() },
                last_at: None,
                last_name: None,
                changes: 0,
                kick_at: None,
            },
        }
    }

    fn is_kick(&self, name: &str) -> bool {
        self.cfg.kick_poses.iter().any(|k| k == name)
    }

    /// May `name` be shown at `now`? Does not record.
    pub fn allow(&self, name: &str, now: f64) -> bool {
        if !now.is_finite() {
            return false;
        }
        if self.is_kick(name) {
            // fail closed: a clock that went backwards counts as inside the gap
            if let Some(k) = self.kick_at {
                if now < k || now - k < self.cfg.kick_gap {
                    return false;
                }
            }
        }
        if self.last_name.as_deref() == Some(name) {
            return true; // a refresh of the same pose
        }
        if self.changes >= self.cfg.max_per_turn {
            return false;
        }
        if let Some(l) = self.last_at {
            if now >= l && now - l < self.cfg.min_gap {
                return false;
            }
        }
        true
    }

    /// Record that `name` was shown at `now`.
    pub fn record(&mut self, name: &str, now: f64) {
        if !now.is_finite() {
            return;
        }
        if self.last_name.as_deref() != Some(name) {
            self.changes += 1;
            self.last_name = Some(name.to_owned());
            self.last_at = Some(now);
        }
        if self.is_kick(name) {
            self.kick_at = Some(now);
        }
    }

    /// `allow` then `record`.
    pub fn try_show(&mut self, name: &str, now: f64) -> bool {
        if self.allow(name, now) {
            self.record(name, now);
            true
        } else {
            false
        }
    }

    /// A new turn begins: the per-turn count and the remembered pose reset.
    /// The gaps measured in time carry over.
    pub fn new_turn(&mut self) {
        self.changes = 0;
        self.last_name = None;
    }

    /// Forget everything (guest change, lid, sleep, forget commands, reconnect).
    pub fn reset(&mut self) {
        self.changes = 0;
        self.last_name = None;
        self.last_at = None;
        self.kick_at = None;
    }

    pub fn changes_this_turn(&self) -> u32 {
        self.changes
    }
}

impl Default for Dwell {
    fn default() -> Self {
        Dwell::new()
    }
}

// ---------------------------------------------------------------------------
// Reachability
// ---------------------------------------------------------------------------

/// One name of the 89: its group, where the director can use it, and whether
/// the face has it live.
#[derive(Clone, Debug, PartialEq)]
pub struct Reach {
    pub name: String,
    pub group: String,
    /// Every (family, rung) that uses the name; empty when the director never emits it.
    pub uses: Vec<(Family, usize)>,
    pub live: bool,
}

impl Reach {
    pub fn family(&self) -> Option<Family> {
        self.uses.first().map(|u| u.0)
    }

    pub fn rung(&self) -> Option<usize> {
        self.uses.first().map(|u| u.1)
    }

    pub fn status(&self) -> &'static str {
        if self.live {
            "live"
        } else {
            "waiting"
        }
    }
}

impl Table {
    /// For each of the 89 names (sheet order): group, family and rung uses,
    /// live (in `live_names`) or waiting for promotion.
    pub fn reachability(&self, live_names: &[&str]) -> Vec<Reach> {
        let mut out = Vec::new();
        for (group, names) in &self.groups {
            for name in names {
                let mut uses = Vec::new();
                for f in Family::ALL {
                    for (r, rung) in self.ladders[f.idx()].rungs.iter().enumerate() {
                        if &rung.pose == name {
                            uses.push((f, r));
                        }
                    }
                }
                out.push(Reach {
                    name: name.clone(),
                    group: group.clone(),
                    uses,
                    live: live_names.contains(&name.as_str()),
                });
            }
        }
        out
    }
}

/// `Table::reachability` over the embedded table (empty if it is inert).
pub fn reachability(live_names: &[&str]) -> Vec<Reach> {
    table().map(|t| t.reachability(live_names)).unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tests (host)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};

    const FACE_JSON: &str = include_str!("../../face/concept-expressions.json");
    const SHEET_JSON: &str = include_str!("../../../docs/design/emotion-director-groups.json");

    fn t() -> &'static Table {
        table().expect("the embedded table must validate")
    }

    fn face_names() -> Vec<String> {
        let v: Value = serde_json::from_str(FACE_JSON).unwrap();
        v["poses"].as_array().unwrap().iter().map(|p| p["name"].as_str().unwrap().to_owned()).collect()
    }

    fn sheet() -> Vec<(String, String)> {
        let v: Value = serde_json::from_str(SHEET_JSON).unwrap();
        v.as_array()
            .unwrap()
            .iter()
            .map(|r| (r["name"].as_str().unwrap().to_owned(), r["group"].as_str().unwrap().to_owned()))
            .collect()
    }

    fn day() -> Ctx {
        Ctx::default()
    }

    fn cue(f: Family, s: f64) -> Cue {
        Cue::new(f, s)
    }

    fn read(text: &str) -> Option<Family> {
        read_clause(text).map(|c| c.family)
    }

    // -- table validity ----------------------------------------------------

    #[test]
    fn embedded_table_parses_and_validates() {
        let table = t();
        assert!(table.lexicon_len() > 40);
        for f in Family::ALL {
            assert!(!table.ladder(f).is_empty(), "{}", f.id());
        }
    }

    #[test]
    fn contract_ladders_are_exactly_as_agreed() {
        let want: [(Family, &[&str]); 10] = [
            (Family::Warm, &["pleased", "joy", "excited"]),
            (Family::Humour, &["amused", "giggle", "laugh"]),
            (Family::Curious, &["curiosity", "idea"]),
            (Family::Doubt, &["skepticism", "smug", "annoyed"]),
            (Family::Worry, &["concern", "sad", "disappointed"]),
            (Family::Surprise, &["surprise", "gasp", "shocked"]),
            (Family::Thanks, &["pleased", "grateful"]),
            (Family::Apology, &["concern", "guilty"]),
            (Family::Support, &["reassuring", "encouraging"]),
            (Family::Calm, &["neutral"]),
        ];
        for (f, ladder) in want {
            assert_eq!(t().ladder(f), ladder, "{}", f.id());
        }
    }

    #[test]
    fn every_ladder_pose_is_in_the_face_catalogue_and_group_a_or_a_star() {
        let face: HashSet<String> = face_names().into_iter().collect();
        assert_eq!(face.len(), 89);
        let groups: HashMap<String, String> = sheet().into_iter().collect();
        for f in Family::ALL {
            for pose in t().ladder(f) {
                assert!(face.contains(pose), "{pose} is not a face pose");
                let g = groups.get(pose).unwrap_or_else(|| panic!("{pose} not in the sheet"));
                assert!(g == "a" || g == "a*", "{pose} is in group {g}");
            }
        }
    }

    #[test]
    fn embedded_groups_match_the_sheet_and_the_face_exactly() {
        let sheet = sheet();
        assert_eq!(sheet.len(), 89);
        for (name, group) in &sheet {
            assert_eq!(t().group_of(name), Some(group.as_str()), "{name}");
        }
        let face: HashSet<String> = face_names().into_iter().collect();
        let names: HashSet<String> = sheet.iter().map(|(n, _)| n.clone()).collect();
        assert_eq!(face, names);
    }

    #[test]
    fn blocklist_holds_c0_b_and_c1() {
        for n in ["sick", "queasy", "p_sneeze", "p_hiccup", "sleepy", "asleep", "tired", "muted", "listening", "thinking", "love", "ko"] {
            assert!(t().is_blocked(n), "{n}");
        }
        for (name, group) in sheet() {
            let blocked = matches!(group.as_str(), "b" | "c0" | "c1");
            assert_eq!(t().is_blocked(&name), blocked, "{name} ({group})");
        }
    }

    #[test]
    fn a_bad_table_is_refused_not_repaired() {
        let bad = TABLE_JSON.replace("\"pose\": \"joy\"", "\"pose\": \"sleepy\"");
        assert!(Table::parse(&bad).is_err());
        let bad = TABLE_JSON.replace("\"pose\": \"guilty\"", "\"pose\": \"sick\"");
        assert!(Table::parse(&bad).is_err());
        let bad = TABLE_JSON.replace("\"min_intensity\": 0.55", "\"min_intensity\": 0.4");
        assert!(Table::parse(&bad).is_err());
        assert!(Table::parse("{").is_err());
        assert!(Table::parse("{}").is_err());
    }

    // -- never c0 or b -----------------------------------------------------

    #[test]
    fn no_choice_is_ever_a_blocked_name() {
        let table = t();
        let sources = [Source::Heard, Source::Reply, Source::Event, Source::Job, Source::Tag, Source::Drive];
        let mut seen = HashSet::new();
        for f in Family::ALL {
            for step in 0..=20 {
                let s = step as f64 / 20.0;
                for src in sources {
                    if let Some(c) = table.choose(&[(src, cue(f, s))], &day()) {
                        assert!(!table.is_blocked(c.name), "{} -> {}", f.id(), c.name);
                        assert_ne!(c.name, "sleepy");
                        seen.insert(c.name.to_owned());
                        if let Some(a) = table.afterglow(&c) {
                            assert!(!table.is_blocked(a.name));
                        }
                    }
                }
                if let Some(c) = table.wire_choice(f.id(), s, None) {
                    assert!(!table.is_blocked(c.name));
                    seen.insert(c.name.to_owned());
                }
            }
        }
        // every ladder pose is reachable from some strength
        for f in Family::ALL {
            for p in table.ladder(f) {
                assert!(seen.contains(p), "{p} unreachable");
            }
        }
    }

    // -- clause reader -----------------------------------------------------

    #[test]
    fn clause_reader_positives() {
        assert_eq!(read("Thank you for telling me."), Some(Family::Thanks));
        assert_eq!(read("Thanks, that is kind."), Some(Family::Thanks));
        assert_eq!(read("You're welcome."), Some(Family::Warm));
        assert_eq!(read("I'm sorry."), Some(Family::Apology));
        assert_eq!(read("I am sorry about that."), Some(Family::Apology));
        assert_eq!(read("I couldn't find it."), Some(Family::Worry));
        assert_eq!(read("I could not open it."), Some(Family::Worry));
        assert_eq!(read("One is missing."), Some(Family::Worry));
        assert_eq!(read("One of them was missing!"), Some(Family::Worry));
        assert_eq!(read("Haha, good one."), Some(Family::Humour));
        assert_eq!(read("Just kidding."), Some(Family::Humour));
        assert_eq!(read("Wow, that is a lot of boxes."), Some(Family::Surprise));
        assert_eq!(read("Oh my goodness."), Some(Family::Surprise));
        assert_eq!(read("Good news, it saved."), Some(Family::Warm));
        assert_eq!(read("I have an idea."), Some(Family::Curious));
        assert_eq!(read("I doubt that."), Some(Family::Doubt));
        assert_eq!(read("You can do it."), Some(Family::Support));
        assert_eq!(read("I'm sorry to hear that."), Some(Family::Worry));
        // the two existing clause_cue patterns keep their old reach
        assert_eq!(read("I can't remember."), Some(Family::Worry));
        assert_eq!(read("I cannot save it."), Some(Family::Worry));
    }

    #[test]
    fn clause_reader_is_ascii_case_insensitive_and_reads_curly_apostrophes() {
        assert_eq!(read("I COULDN\u{2019}T FIND IT"), Some(Family::Worry));
        assert_eq!(read("hAhA"), Some(Family::Humour));
        assert_eq!(read("  THANK YOU  "), Some(Family::Thanks));
    }

    #[test]
    fn clause_reader_negatives_sense_and_body_claims() {
        for s in [
            "I see a bird.",
            "I can see a bird by the window.",
            "I can hear rain.",
            "I hear a knock.",
            "I feel warm.",
            "I feel hot.",
            "I looked at it.",
            "I looked and it is a cable.",
            "I'm tired.",
            "I am a little sleepy.",
            "My battery is low.",
            "It is cold in here.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
    }

    #[test]
    fn clause_reader_negatives_neutral_facts() {
        for s in [
            "The file has two entries.",
            "It is half past four.",
            "Your meeting is at nine.",
            "The disk is ninety percent full.",
            "Here is what I found.",
            "",
            "   ",
            "...",
        ] {
            assert_eq!(read_clause(s), None, "{s:?}");
        }
    }

    #[test]
    fn clause_reader_pins_of_the_old_regex_hold() {
        // tests/test_mind.py: quoted speech and negated loss do not react
        assert_eq!(read_clause("\"I could not find it,\" she said."), None);
        assert_eq!(read_clause("\u{201c}I could not find it,\u{201d} she said."), None);
        assert_eq!(read_clause("None of them is missing."), None);
        assert_eq!(read_clause("One is missing, I think, maybe."), None);
        // the first pattern is anchored, the second is the whole clause
        assert_eq!(read_clause("Well, I couldn't find it."), None);
        assert_eq!(read_clause("I couldn't finder it."), None);
        assert_eq!(read_clause("One of them is missing.."), None);
    }

    #[test]
    fn longest_pattern_wins_then_strength() {
        let (c, rule) = t().read_clause_rule("I'm sorry to hear that.").unwrap();
        assert_eq!(c.family, Family::Worry);
        assert_eq!(rule, "worry-sorry-hear");
        let (c, rule) = t().read_clause_rule("Oh my goodness, wow.").unwrap();
        assert_eq!(c.family, Family::Surprise);
        assert_eq!(rule, "surprise-oh-my-gosh");
    }

    #[test]
    fn clause_scan_is_capped_in_words_and_bytes() {
        let filler = "alpha ".repeat(40);
        assert_eq!(read_clause(&format!("{filler}lol")), None, "the 41st word is not scanned");
        let almost = "alpha ".repeat(38);
        assert_eq!(read_clause(&format!("{almost}lol")), Some(cue(Family::Humour, 0.6)));
        let long = format!("{}wow", "a".repeat(450));
        assert_eq!(read_clause(&long), None);
        // a multi-byte character straddling the byte cap does not panic
        for pad in 390..=400 {
            let s = format!("{}\u{e9}\u{2019}\u{4e2d} wow", "b".repeat(pad));
            let _ = read_clause(&s);
        }
        let s = "\u{4e2d}".repeat(300);
        assert_eq!(read_clause(&s), None);
    }

    // -- tags and blend ----------------------------------------------------

    #[test]
    fn tags_map_and_neutral_and_sleepy_count_nothing() {
        let table = t();
        assert_eq!(table.tag_cue("neutral"), None);
        assert_eq!(table.tag_cue("sleepy"), None);
        assert_eq!(table.tag_cue("unknown"), None);
        assert_eq!(table.tag_cue("joy").unwrap().family, Family::Warm);
        assert_eq!(table.tag_cue("curiosity").unwrap().family, Family::Curious);
        assert_eq!(table.tag_cue("surprise").unwrap().family, Family::Surprise);
        assert_eq!(table.tag_cue("skepticism").unwrap().family, Family::Doubt);
        assert_eq!(table.tag_cue("concern").unwrap().family, Family::Worry);
        assert_eq!(table.tag_cue("JOY").unwrap().family, Family::Warm);
    }

    #[test]
    fn neutral_tag_adds_nothing() {
        let table = t();
        assert_eq!(table.choose(&[], &day()), None);
        let calm_tag = (Source::Tag, cue(Family::Calm, 1.0));
        assert_eq!(table.choose(&[calm_tag], &day()), None);
        let reply = (Source::Reply, cue(Family::Humour, 0.65));
        assert_eq!(table.choose(&[reply, calm_tag], &day()), table.choose(&[reply], &day()));
    }

    #[test]
    fn joy_tag_alone_wins_mildly() {
        let table = t();
        let tag = table.tag_cue("joy").unwrap();
        let c = table.choose(&[(Source::Tag, tag)], &day()).unwrap();
        assert_eq!(c.name, "pleased");
        assert_eq!(c.family, Family::Warm);
        assert_eq!(c.source, Source::Tag);
        assert!(c.intensity >= 0.4 && c.intensity < 0.9);
    }

    #[test]
    fn clear_text_outvotes_the_tag() {
        let table = t();
        let tag = (Source::Tag, table.tag_cue("curiosity").unwrap());
        let text = (Source::Reply, cue(Family::Humour, 0.65));
        let c = table.choose(&[tag, text], &day()).unwrap();
        assert_eq!(c.family, Family::Humour);
        assert_eq!(c.name, "giggle");
        // the same tag with weak text keeps the tag
        let weak = (Source::Reply, cue(Family::Humour, 0.04));
        let c = table.choose(&[tag, weak], &day()).unwrap();
        assert_eq!(c.family, Family::Curious);
    }

    #[test]
    fn text_never_flips_a_positive_tag_to_a_negative_family() {
        let table = t();
        let joy = (Source::Tag, table.tag_cue("joy").unwrap());
        // negative text overrules the positive tag: nothing is shown, not the
        // tag's pleased face over sorrowful words (a wrong reaction is worse than none)
        for neg in [Family::Worry, Family::Doubt, Family::Apology] {
            for src in [Source::Reply, Source::Heard] {
                assert_eq!(table.choose(&[joy, (src, cue(neg, 1.0))], &day()), None, "{neg:?} from {src:?}");
                // the order of the votes does not matter
                assert_eq!(table.choose(&[(src, cue(neg, 1.0)), joy], &day()), None, "{neg:?} from {src:?}");
            }
        }
        // positive text can still lift a positive tag
        let c = table.choose(&[joy, (Source::Reply, cue(Family::Humour, 0.9))], &day()).unwrap();
        assert_eq!(c.family, Family::Humour);
        // positive text beside the overruled pair still shows by itself
        let c = table
            .choose(&[joy, (Source::Reply, cue(Family::Worry, 1.0)), (Source::Reply, cue(Family::Humour, 0.9))], &day())
            .unwrap();
        assert_eq!(c.family, Family::Humour);
        // a negative tag is outvoted by positive text (the asymmetry is one-way)
        let concern = (Source::Tag, table.tag_cue("concern").unwrap());
        let c = table.choose(&[concern, (Source::Reply, cue(Family::Warm, 0.6))], &day()).unwrap();
        assert_eq!(c.family, Family::Warm);
        // an event row is real: a failed job still wins over a joy tag
        let c = table.choose(&[joy, (Source::Job, cue(Family::Worry, 0.8))], &day());
        assert_eq!(c.unwrap().family, Family::Worry);
        // ... and it still shows when negative text overruled the tag in the same blend
        let c = table.choose(&[joy, (Source::Reply, cue(Family::Doubt, 1.0)), (Source::Job, cue(Family::Worry, 0.8))], &day());
        assert_eq!(c.unwrap().family, Family::Worry);
        // negative text with no positive tag works as normal
        let c = table.choose(&[(Source::Reply, cue(Family::Worry, 0.5))], &day()).unwrap();
        assert_eq!(c.name, "concern");
        // a curious or surprised tag is not positive, so negative text keeps its say
        let curious = (Source::Tag, table.tag_cue("curiosity").unwrap());
        let c = table.choose(&[curious, (Source::Reply, cue(Family::Worry, 0.9))], &day()).unwrap();
        assert_eq!(c.family, Family::Worry);
    }

    #[test]
    fn a_drive_alone_never_fires_and_only_nudges() {
        let table = t();
        for f in Family::ALL {
            assert_eq!(table.choose(&[(Source::Drive, cue(f, 1.0))], &day()), None, "{}", f.id());
        }
        let text = (Source::Reply, cue(Family::Warm, 0.4));
        let plain = table.choose(&[text], &day()).unwrap();
        let nudged = table.choose(&[text, (Source::Drive, cue(Family::Warm, 1.0))], &day()).unwrap();
        assert!(nudged.intensity > plain.intensity);
        assert_eq!(nudged.name, plain.name);
        // a tag-led choice is capped, nudge or not
        let tag = (Source::Tag, cue(Family::Warm, 0.4));
        let nudged = table.choose(&[tag, (Source::Drive, cue(Family::Warm, 1.0))], &day()).unwrap();
        assert!(nudged.intensity <= FACE_CUE_INTENSITY + 1e-9);
        // a drive cannot beat a real source
        let c = table
            .choose(&[(Source::Reply, cue(Family::Humour, 0.5)), (Source::Drive, cue(Family::Worry, 1.0))], &day())
            .unwrap();
        assert_eq!(c.family, Family::Humour);
    }

    #[test]
    fn agreeing_sources_raise_strength_and_hold_follows_the_lead() {
        let table = t();
        let alone = table.choose(&[(Source::Reply, cue(Family::Warm, 0.5))], &day()).unwrap();
        let both = table
            .choose(&[(Source::Reply, cue(Family::Warm, 0.5)), (Source::Tag, cue(Family::Warm, 0.55))], &day())
            .unwrap();
        assert!(both.intensity > alone.intensity);
        assert_eq!(alone.source, Source::Reply);
        assert_eq!(alone.hold, 8.0);
        let heard = table.choose(&[(Source::Heard, cue(Family::Worry, 0.5))], &day()).unwrap();
        assert_eq!(heard.hold, 1.2);
        assert_eq!(heard.source, Source::Heard);
    }

    #[test]
    fn heard_words_outvote_reply_words() {
        let table = t();
        let c = table
            .choose(&[(Source::Reply, cue(Family::Humour, 0.6)), (Source::Heard, cue(Family::Support, 0.6))], &day())
            .unwrap();
        assert_eq!(c.family, Family::Support);
    }

    // -- mapping -----------------------------------------------------------

    #[test]
    fn strength_maps_to_rung_and_intensity() {
        let table = t();
        let (r, i) = table.map_strength(Family::Warm, 0.0);
        assert_eq!((r, i), (0, 0.4));
        let (r, i) = table.map_strength(Family::Warm, 0.5);
        assert_eq!(r, 0);
        assert!((i - 0.7).abs() < 1e-9);
        assert_eq!(table.map_strength(Family::Warm, 0.6).0, 1);
        assert_eq!(table.map_strength(Family::Warm, 0.85).0, 2);
        assert_eq!(table.map_strength(Family::Warm, 1.0), (2, 1.0));
        // clamps
        assert_eq!(table.map_strength(Family::Warm, -3.0), (0, 0.4));
        assert_eq!(table.map_strength(Family::Warm, 9.0), (2, 1.0));
        assert_eq!(table.map_strength(Family::Warm, f64::NAN), (0, 0.4));
        // intensity grows with strength within and across rungs
        let mut prev = 0.0;
        for k in 0..=100 {
            let (_, i) = table.map_strength(Family::Worry, k as f64 / 100.0);
            assert!(i >= prev && (0.4..=1.0).contains(&i));
            prev = i;
        }
        // short ladders cap at their last rung
        assert_eq!(table.map_strength(Family::Calm, 1.0).0, 0);
        assert_eq!(table.map_strength(Family::Curious, 1.0).0, 1);
    }

    #[test]
    fn surprise_family_never_goes_below_0_55() {
        let table = t();
        for k in 0..=100 {
            let s = k as f64 / 100.0;
            let (_, i) = table.map_strength(Family::Surprise, s);
            assert!(i >= 0.55, "{s}: {i}");
            let c = table.wire_choice("surprise", s.max(0.3), None).unwrap();
            assert!(c.intensity >= 0.55);
        }
        // reply text reaches the surprise anchor at the floor
        let c = table.choose(&[(Source::Reply, cue(Family::Surprise, 0.55))], &day()).unwrap();
        assert_eq!(c.name, "surprise");
        assert!(c.intensity >= 0.55);
        // other families may go to 0.4
        assert_eq!(table.map_strength(Family::Doubt, 0.0).1, 0.4);
    }

    #[test]
    fn wire_cue_maps_through_the_ladder_and_clamps_the_hold() {
        let table = t();
        let c = table.wire_choice("worry", 0.7, Some(1.0)).unwrap();
        assert_eq!((c.name, c.hold, c.source), ("sad", 1.0, Source::Heard));
        assert_eq!(table.wire_choice("worry", 0.7, Some(99.0)).unwrap().hold, 2.5);
        assert_eq!(table.wire_choice("worry", 0.7, Some(0.0)).unwrap().hold, 0.5);
        assert_eq!(table.wire_choice("worry", 0.7, None).unwrap().hold, 1.2);
        assert_eq!(table.wire_choice("worry", 0.7, Some(f64::NAN)).unwrap().hold, 1.2);
        assert_eq!(table.wire_choice("sleepy", 0.7, None), None);
        assert_eq!(table.wire_choice("joy", 0.7, None), None, "the mind sends family ids, not tags");
        assert_eq!(table.wire_choice("warm", f64::NAN, None), None);
        assert_eq!(table.wire_choice("warm", 0.0, None), None);
        assert_eq!(table.wire_choice("thanks", 0.9, None).unwrap().name, "grateful");
    }

    #[test]
    fn afterglow_is_mild_short_and_never_a_kick() {
        let table = t();
        let c = table.choose(&[(Source::Reply, cue(Family::Warm, 0.9))], &day()).unwrap();
        let a = table.afterglow(&c).unwrap();
        assert_eq!(a.name, c.name);
        assert_eq!(a.intensity, 0.45);
        assert_eq!(a.hold, 1.0);
        // every rung of the surprise ladder is a startle-kick pose: no glow
        for kick in ["surprise", "gasp", "shocked"] {
            let fam = Family::Surprise;
            let rung = table.ladder(fam).iter().position(|p| *p == kick).unwrap();
            let c = Choice { name: kick, intensity: 1.0, hold: 1.0, family: fam, rung, source: Source::Reply };
            assert_eq!(table.afterglow(&c), None, "{kick}");
        }
    }

    // -- gate --------------------------------------------------------------

    fn all_outputs() -> [Output; 4] {
        [Output::Word, Output::Reply, Output::Spontaneous, Output::Job]
    }

    #[test]
    fn plain_day_allows_everything() {
        for o in all_outputs() {
            assert_eq!(gate(&day(), o), Ok(()), "{o:?}");
        }
    }

    #[test]
    fn body_states_veto_everything() {
        let flags: [fn(&mut Ctx); 5] = [
            |c| c.asleep = true,
            |c| c.waking = true,
            |c| c.muted = true,
            |c| c.unwell = true,
            |c| c.lid_blocked = true,
        ];
        for set in flags {
            let mut ctx = day();
            set(&mut ctx);
            for o in all_outputs() {
                assert_eq!(gate(&ctx, o), Err(Veto::Body));
            }
            let votes = [
                (Source::Heard, cue(Family::Worry, 1.0)),
                (Source::Reply, cue(Family::Warm, 1.0)),
                (Source::Tag, cue(Family::Warm, 1.0)),
                (Source::Job, cue(Family::Worry, 1.0)),
                (Source::Drive, cue(Family::Warm, 1.0)),
            ];
            assert_eq!(t().choose(&votes, &ctx), None);
        }
    }

    #[test]
    fn quiet_and_drowsy_silence_everything_spontaneous_but_not_replies() {
        for set in [(|c: &mut Ctx| c.quiet = true) as fn(&mut Ctx), |c| c.drowsy = true] {
            let mut ctx = day();
            set(&mut ctx);
            assert_eq!(gate(&ctx, Output::Spontaneous), Err(Veto::Quiet));
            assert_eq!(gate(&ctx, Output::Job), Err(Veto::Quiet));
            assert_eq!(gate(&ctx, Output::Word), Ok(()));
            assert_eq!(gate(&ctx, Output::Reply), Ok(()));
            let table = t();
            assert_eq!(table.choose(&[(Source::Event, cue(Family::Worry, 1.0))], &ctx), None);
            assert_eq!(table.choose(&[(Source::Job, cue(Family::Worry, 1.0))], &ctx), None);
            assert!(table.choose(&[(Source::Reply, cue(Family::Warm, 0.6))], &ctx).is_some());
            assert!(table.choose(&[(Source::Heard, cue(Family::Worry, 0.6))], &ctx).is_some());
        }
    }

    #[test]
    fn guest_allows_job_rows_only() {
        let ctx = Ctx { guest: true, ..day() };
        assert_eq!(gate(&ctx, Output::Word), Err(Veto::Guest));
        assert_eq!(gate(&ctx, Output::Reply), Err(Veto::Guest));
        assert_eq!(gate(&ctx, Output::Spontaneous), Err(Veto::Guest));
        assert_eq!(gate(&ctx, Output::Job), Ok(()));
        let table = t();
        assert_eq!(table.choose(&[(Source::Heard, cue(Family::Worry, 1.0))], &ctx), None);
        assert_eq!(table.choose(&[(Source::Reply, cue(Family::Warm, 1.0)), (Source::Tag, cue(Family::Warm, 0.55))], &ctx), None);
        assert!(table.choose(&[(Source::Job, cue(Family::Worry, 0.8))], &ctx).is_some());
        // a heard cue is dropped but a job row still passes in the same blend
        let c = table
            .choose(&[(Source::Heard, cue(Family::Humour, 1.0)), (Source::Job, cue(Family::Worry, 0.6))], &ctx)
            .unwrap();
        assert_eq!(c.family, Family::Worry);
    }

    #[test]
    fn announcing_blocks_words_and_replies_but_not_job_rows() {
        let ctx = Ctx { announcing: true, ..day() };
        assert_eq!(gate(&ctx, Output::Reply), Err(Veto::Announcing));
        assert_eq!(gate(&ctx, Output::Word), Err(Veto::Announcing));
        assert_eq!(gate(&ctx, Output::Job), Ok(()));
    }

    #[test]
    fn effects_flag_alone_vetoes_nothing() {
        for fx in [false, true] {
            let ctx = Ctx { effects_on: fx, ..day() };
            for o in all_outputs() {
                assert_eq!(gate(&ctx, o), Ok(()));
            }
        }
    }

    #[test]
    fn gate_properties_over_every_flag_combination() {
        // 2^9 combinations of the nine vetoing flags: nothing slips through
        for bits in 0u32..512 {
            let b = |i: u32| bits & (1 << i) != 0;
            let ctx = Ctx {
                asleep: b(0), waking: b(1), drowsy: b(2), muted: b(3), unwell: b(4),
                lid_blocked: b(5), guest: b(6), announcing: b(7), quiet: b(8), effects_on: false,
            };
            let body = b(0) || b(1) || b(3) || b(4) || b(5);
            for o in all_outputs() {
                let ok = gate(&ctx, o).is_ok();
                if body {
                    assert!(!ok, "{bits:09b} {o:?}");
                }
                if (b(6) || b(7)) && o != Output::Job {
                    assert!(!ok, "{bits:09b} {o:?}");
                }
                if (b(2) || b(8)) && matches!(o, Output::Spontaneous | Output::Job) {
                    assert!(!ok, "{bits:09b} {o:?}");
                }
            }
        }
    }

    // -- dwell -------------------------------------------------------------

    #[test]
    fn dwell_min_gap_between_changes() {
        let mut d = Dwell::new();
        assert!(d.try_show("pleased", 100.0));
        assert!(!d.try_show("curiosity", 100.5));
        assert!(!d.allow("curiosity", 101.19));
        assert!(d.try_show("curiosity", 101.2));
        assert!(d.try_show("concern", 102.4));
    }

    #[test]
    fn dwell_caps_three_changes_per_turn() {
        let mut d = Dwell::new();
        assert!(d.try_show("pleased", 0.0));
        assert!(d.try_show("curiosity", 2.0));
        assert!(d.try_show("concern", 4.0));
        assert_eq!(d.changes_this_turn(), 3);
        assert!(!d.try_show("idea", 6.0), "a fourth change in the turn");
        d.new_turn();
        assert!(d.try_show("idea", 8.0));
    }

    #[test]
    fn dwell_same_pose_is_a_refresh_not_a_change() {
        let mut d = Dwell::new();
        assert!(d.try_show("pleased", 0.0));
        assert!(d.try_show("pleased", 0.2));
        assert!(d.try_show("pleased", 0.4));
        assert_eq!(d.changes_this_turn(), 1);
        assert!(d.try_show("curiosity", 2.0));
    }

    #[test]
    fn dwell_startle_kick_poses_not_twice_within_ten_seconds() {
        let mut d = Dwell::new();
        assert!(d.try_show("surprise", 0.0));
        assert!(!d.allow("surprise", 5.0));
        assert!(!d.allow("shocked", 5.0), "another kick pose is still a kick");
        assert!(!d.allow("startled", 9.99));
        assert!(!d.allow("scared", 9.99));
        // other poses are unaffected
        assert!(d.try_show("pleased", 5.0));
        assert!(d.allow("shocked", 10.0));
        d.new_turn();
        assert!(!d.allow("surprise", 6.0), "a new turn does not clear the startle spacing");
        assert!(d.try_show("surprise", 10.5));
        assert!(!d.allow("surprise", 11.0));
    }

    #[test]
    fn dwell_reset_and_clock_oddities() {
        let mut d = Dwell::new();
        assert!(d.try_show("surprise", 50.0));
        d.reset();
        assert!(d.try_show("surprise", 50.1));
        // non-finite time is refused and records nothing
        assert!(!d.allow("pleased", f64::NAN));
        assert!(!d.allow("pleased", f64::INFINITY));
        d.record("pleased", f64::NAN);
        assert_eq!(d.changes_this_turn(), 1);
        // time running backwards ignores the old history
        let mut d = Dwell::new();
        assert!(d.try_show("pleased", 1000.0));
        assert!(d.try_show("curiosity", 3.0));
    }

    // -- determinism -------------------------------------------------------

    #[test]
    fn repeated_runs_are_identical() {
        let table = t();
        let text = "Haha, good one, and thanks for waiting!";
        let first = (table.read_clause(text), table.read_clause_rule(text).map(|r| r.1.to_owned()));
        let votes = [
            (Source::Reply, table.read_clause(text).unwrap()),
            (Source::Tag, table.tag_cue("joy").unwrap()),
            (Source::Drive, cue(Family::Warm, 0.5)),
        ];
        let c1 = table.choose(&votes, &day());
        for _ in 0..200 {
            assert_eq!((table.read_clause(text), table.read_clause_rule(text).map(|r| r.1.to_owned())), first);
            assert_eq!(table.choose(&votes, &day()), c1);
        }
        // vote order does not matter
        let mut rev = votes;
        rev.reverse();
        assert_eq!(table.choose(&rev, &day()), c1);
        let mut d1 = Dwell::new();
        let mut d2 = Dwell::new();
        for (name, at) in [("pleased", 0.0), ("curiosity", 0.5), ("surprise", 2.0), ("surprise", 3.0), ("concern", 3.5)] {
            assert_eq!(d1.try_show(name, at), d2.try_show(name, at));
        }
    }

    // -- reachability ------------------------------------------------------

    #[test]
    fn reachability_covers_all_89_names() {
        let live = ["neutral", "surprise", "curiosity", "skepticism", "joy", "pleased", "concern", "listening"];
        let rows = reachability(&live);
        assert_eq!(rows.len(), 89);
        let names: HashSet<&str> = rows.iter().map(|r| r.name.as_str()).collect();
        assert_eq!(names.len(), 89);
        let by: HashMap<&str, &Reach> = rows.iter().map(|r| (r.name.as_str(), r)).collect();
        let joy = by["joy"];
        assert_eq!((joy.group.as_str(), joy.family(), joy.rung(), joy.status()), ("a", Some(Family::Warm), Some(1), "live"));
        let excited = by["excited"];
        assert_eq!((excited.family(), excited.rung(), excited.status()), (Some(Family::Warm), Some(2), "waiting"));
        // a pose shared by two ladders lists both uses
        assert_eq!(by["pleased"].uses, vec![(Family::Warm, 0), (Family::Thanks, 0)]);
        assert_eq!(by["concern"].uses, vec![(Family::Worry, 0), (Family::Apology, 0)]);
        // group b and c are never used by a ladder, whether live or not
        assert_eq!(by["listening"].group, "b");
        assert!(by["listening"].uses.is_empty() && by["listening"].live);
        assert_eq!(by["sick"].group, "c0");
        assert!(by["sick"].uses.is_empty() && !by["sick"].live);
        assert_eq!(by["love"].group, "c1");
        // every ladder pose shows up as used
        for f in Family::ALL {
            for p in t().ladder(f) {
                assert!(!by[p].uses.is_empty(), "{p}");
                assert!(matches!(by[p].group.as_str(), "a" | "a*"));
            }
        }
        // none live: everything waits
        assert!(reachability(&[]).iter().all(|r| r.status() == "waiting"));
    }

    // -- adversarial review fixes (Rust half) -------------------------------

    fn strength_of(text: &str) -> Option<f64> {
        read_clause(text).map(|c| c.strength)
    }

    #[test]
    fn refusals_and_statements_do_not_startle() {
        for s in [
            "There is no way to recover it.",
            "There is no way!",
            "No way to undo this.",
            "No way to tell from here.",
            "Please note I can't believe that.",
            "I said oh my to myself.",
            "Oh my word, it is long.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // the anchored forms still read
        assert_eq!(read("No way!"), Some(Family::Surprise));
        assert_eq!(read("No way."), Some(Family::Surprise));
        assert_eq!(read("Oh my, that is a lot."), Some(Family::Surprise));
        assert_eq!(read("Oh my!"), Some(Family::Surprise));
        assert_eq!(read("Oh my goodness."), Some(Family::Surprise));
        // an opening "i can't believe" is weak (0.5 or less)
        let s = strength_of("I can't believe it!").unwrap();
        assert!(s <= 0.5, "{s}");
        assert_eq!(read("I can't believe it!"), Some(Family::Surprise));
    }

    #[test]
    fn negation_within_a_few_tokens_blocks_praise_and_curiosity() {
        for s in [
            "That is not a good idea.",
            "I don't think that's a good idea.",
            "I don't have good news yet.",
            "I don't have an idea what that is.",
            "I have an idea what that is.",
            "Not awesome at all.",
            "That was never fantastic.",
            "There is no great news.",
            "Without wonderful results, it is hard.",
            "I can't say that was brilliant.",
            "It isn't wonderful.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // the guard covers the negative rows too
        assert_eq!(read("It is not that's a shame, ok"), None);
        // positives stay
        assert_eq!(read("That is a good idea."), Some(Family::Warm));
        assert_eq!(read("Good idea!"), Some(Family::Warm));
        assert_eq!(read("That is awesome."), Some(Family::Warm));
        assert_eq!(read("I have an idea."), Some(Family::Curious));
        // sentence punctuation ends the look-back guard, but a positive reading
        // still refuses a negator anywhere else in the clause
        assert_eq!(read_clause("No. Great news!"), None);
        assert_eq!(read("Okay. Great news!"), Some(Family::Warm));
        // a praise word needs the clause start (or `that's` right before it)
        assert_eq!(read_clause("Not now! Wonderful."), None);
        assert_eq!(read("Wonderful, thank you."), Some(Family::Warm));
        // too far back to count (more than four tokens before the match), but
        // the contrast word still splits the clause
        assert_eq!(read_clause("Not that it matters at all, but it is wonderful."), None);
        assert_eq!(read_clause("Not that it matters at all, it is wonderful."), None);
        assert_eq!(read("Honestly, it is wonderful."), Some(Family::Warm));
        // a phrase that carries its own negation still reads
        assert_eq!(read("Don't worry about it."), Some(Family::Support));
        assert_eq!(read("No worries."), Some(Family::Support));
        assert_eq!(read("I'm not convinced."), Some(Family::Doubt));
        assert_eq!(read("I couldn't find it."), Some(Family::Worry));
    }

    #[test]
    fn non_ascii_letters_are_word_characters_not_separators() {
        for s in [
            "Ha\u{ef}ku is a poem form.",
            "Wow\u{e9}s are not a thing.",
            "Hi\u{e9}roglyph is a word.",
            "Thanks\u{e7}a is not a word.",
            "Lol\u{f1}a",
            "Say wow\u{f1}o.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // non-ASCII punctuation, dashes, ellipses and no-break spaces still separate
        assert_eq!(read("Wow\u{2014}that is a lot."), Some(Family::Surprise));
        assert_eq!(read("Wow\u{2026} that is a lot."), Some(Family::Surprise));
        assert_eq!(read("Thank\u{a0}you for waiting."), Some(Family::Thanks));
        assert_eq!(read("Haha\u{3002}"), Some(Family::Humour));
        // a foreign word beside a table word is just another word
        assert_eq!(read("\u{4e2d}\u{6587} haha"), Some(Family::Humour));
    }

    #[test]
    fn the_byte_cap_never_cuts_a_word() {
        let cap = t().scan_bytes;
        assert_eq!(cap, 400);
        // 397 bytes of filler, then a word that begins with a table word
        // (19 words, well inside the word cap, so only the byte cap acts)
        let filler = format!("{}{}", "abcdefghijklmnopqrst ".repeat(18), "xxxxxxxxxxxxxxxxxx ");
        assert_eq!(filler.len(), cap - 3);
        for tail in ["lolcats and more", "lol\u{e9}", "lol'", "lolz"] {
            assert_eq!(read_clause(&format!("{filler}{tail}")), None, "{tail}");
        }
        // the same words cut cleanly at a boundary still read
        assert_eq!(read(&format!("{filler}lol it is")), Some(Family::Humour));
        assert_eq!(read(&format!("{filler}lol, it is")), Some(Family::Humour));
        assert_eq!(read(&format!("{filler}lol")), Some(Family::Humour));
        // a cut inside a longer word further along cannot form a table word either
        for pad in 380..=400 {
            let s = format!("{}{} thanks again", "q".repeat(pad), "wow");
            let _ = read_clause(&s);
        }
        // a table word of three letters cut out of a longer word at the end of the cap
        let lol = format!("{}{}", "abcdefghijklmnopqrst ".repeat(18), "xxxxxxxxxxxxxxxxxx ");
        assert_eq!(lol.len(), cap - 3);
        assert_eq!(read_clause(&format!("{lol}lollipop")), None);
        assert_eq!(read(&format!("{lol}lol, that is funny")), Some(Family::Humour));
    }

    #[test]
    fn bare_ha_heh_names_and_compounds_do_not_laugh_or_greet() {
        for s in [
            "Ha Long Bay is in Vietnam.",
            "Ha!",
            "Heh.",
            "Heh, that works.",
            "Hi-res audio is supported.",
            "HI is the state code for Hawaii.",
            "Thanks-giving is next week.",
            "Hey-day is a word.",
            "The anti-awesome filter is on.",
            "Wow-factor is a term.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // real laughter and a real greeting stay
        assert_eq!(read("Haha!"), Some(Family::Humour));
        assert_eq!(read("Ha ha, good one."), Some(Family::Humour));
        assert_eq!(read("Ha, ha."), Some(Family::Humour));
        assert_eq!(read("HAHA"), Some(Family::Humour));
        assert_eq!(read("Hehe."), Some(Family::Humour));
        assert_eq!(read("Hi, I found it."), Some(Family::Warm));
        assert_eq!(read("Hi!"), Some(Family::Warm));
        assert_eq!(read("Hello there."), Some(Family::Warm));
        assert_eq!(read("WOW!"), Some(Family::Surprise));
        assert_eq!(read("THANK YOU!"), Some(Family::Thanks));
    }

    #[test]
    fn keep_going_and_well_done_are_start_rows_and_weak() {
        for s in [
            "Keep going straight for two blocks.",
            "Keep going straight.",
            "The steak is well done.",
            "Well done steak is dry.",
            "Then keep it up on the shelf.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        for s in ["Well done!", "Well done, Richard.", "Keep going!", "Keep going, you are close.", "Keep it up."] {
            assert_eq!(read(s), Some(Family::Support), "{s}");
            assert!(strength_of(s).unwrap() <= 0.4, "{s}");
            let c = t().choose(&[(Source::Reply, read_clause(s).unwrap())], &day()).unwrap();
            assert_eq!(c.name, "reassuring", "{s}: below the encouraging rung");
        }
        // plain praise stays its own row, still reaching encouraging, but only
        // at the clause start with a break after (a noun phrase like "good job
        // market" is not praise)
        assert_eq!(read_clause("Great job on the list."), None);
        assert_eq!(read_clause("Good job market for nurses."), None);
        assert_eq!(read("Nice work!"), Some(Family::Support));
        assert_eq!(read("Good job, Richard."), Some(Family::Support));
        let (_, rule) = t().read_clause_rule("Great job!").unwrap();
        assert_eq!(rule, "support-praise");
        let (_, rule) = t().read_clause_rule("Keep going!").unwrap();
        assert_eq!(rule, "support-keep-going");
        assert_eq!(read("That was not a good job."), None);
    }

    #[test]
    fn oh_no_needs_no_comma_and_no_polite_refusal() {
        for s in [
            "Oh, no thanks, I'm fine.",
            "Oh no thanks, I'm fine.",
            "Oh no thank you.",
            "Oh no that's fine.",
            "Uh, oh.",
            "Say oh no and go.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        assert_eq!(read("Oh no!"), Some(Family::Worry));
        assert_eq!(read("Oh no, the file is gone."), Some(Family::Worry));
        assert_eq!(read("Uh oh."), Some(Family::Worry));
        assert_eq!(read("Uh oh, that did not save."), Some(Family::Worry));
    }

    #[test]
    fn row_attributes_are_validated() {
        let bad = TABLE_JSON.replace("\"after\": \"break\"", "\"after\": \"never\"");
        assert!(Table::parse(&bad).is_err());
        let bad = TABLE_JSON.replace("\"tight\": true", "\"tight\": \"yes\"");
        assert!(Table::parse(&bad).is_err());
    }

    #[test]
    fn dwell_going_backwards_keeps_the_startle_spacing() {
        let mut d = Dwell::new();
        assert!(d.try_show("surprise", 100.0));
        assert!(!d.allow("surprise", 50.0), "a clock that went back is still inside the gap");
        assert!(!d.allow("shocked", 99.0));
        assert!(!d.allow("gasp", 0.0));
        // ordinary poses are not frozen by the same jump
        assert!(d.try_show("pleased", 3.0));
        // reset clears it
        d.reset();
        assert!(d.allow("surprise", 50.0));
    }

    #[test]
    fn gasp_is_a_startle_kick_pose() {
        assert!(t().dwell.kick_poses.iter().any(|k| k == "gasp"));
        for rung in t().ladder(Family::Surprise) {
            assert!(t().dwell.kick_poses.iter().any(|k| k == rung), "{rung} is not a kick pose");
        }
        let mut d = Dwell::new();
        assert!(d.try_show("gasp", 0.0));
        assert!(!d.allow("gasp", 5.0), "a gasp cannot repeat inside ten seconds");
        assert!(!d.allow("surprise", 5.0));
        assert!(!d.allow("shocked", 9.9));
        assert!(d.allow("shocked", 10.0));
    }

    #[test]
    fn wire_choice_honours_min_score() {
        let table = t();
        let w = table.weight(Source::Heard);
        assert!(table.wire_choice("worry", 0.001, None).is_none());
        assert!(table.wire_choice("worry", 0.04, None).is_none());
        let edge = table.min_score / w;
        assert!(table.wire_choice("worry", edge * 0.99, None).is_none());
        assert!(table.wire_choice("worry", edge, None).is_some());
        assert!(table.wire_choice("worry", edge * 1.5, None).is_some());
        // the same threshold as `choose` for a heard vote
        for k in 0..=40 {
            let s = k as f64 / 400.0;
            let via_wire = table.wire_choice("worry", s, None).is_some();
            let via_choose = table.choose(&[(Source::Heard, cue(Family::Worry, s))], &day()).is_some();
            assert_eq!(via_wire, via_choose, "{s}");
        }
        // calm at a faint strength shows nothing too
        assert!(table.wire_choice("calm", 0.02, None).is_none());
    }

    #[test]
    fn leading_whitespace_is_scanned_only_up_to_the_byte_cap() {
        use std::time::Instant;
        let spaces = " ".repeat(20_000_000);
        let text = format!("{spaces}wow");
        let start = Instant::now();
        assert_eq!(read_clause(&text), None);
        let took = start.elapsed();
        assert!(took.as_millis() < 5, "{took:?}: the scan must not walk the whole run of spaces");
        // whitespace inside the cap still lets the clause read
        assert_eq!(read(&format!("{}wow", " ".repeat(100))), Some(Family::Surprise));
        assert_eq!(read_clause(&" ".repeat(t().scan_bytes + 5)), None);
        // a quote after leading space is still quoted speech
        assert_eq!(read_clause("   \"wow\""), None);
        // unicode whitespace within the cap
        assert_eq!(read("\u{3000}\u{a0}wow"), Some(Family::Surprise));
    }

    #[test]
    fn guest_mode_blocks_reply_clauses_by_design() {
        let ctx = Ctx { guest: true, ..day() };
        let table = t();
        let c = table.read_clause("Thank you for waiting.").unwrap();
        assert_eq!(table.choose(&[(Source::Reply, c)], &ctx), None);
        assert_eq!(table.choose(&[(Source::Reply, c), (Source::Tag, table.tag_cue("joy").unwrap())], &ctx), None);
        assert!(table.choose(&[(Source::Reply, c)], &day()).is_some());
    }

    // -- round 3: structural rules ------------------------------------------

    /// The Rust reviewer's 40 adversarial replies (round 2), then 6 controls.
    const REVIEW_SILENT: &[&str] = &[
        r#"That's kind of a long story, and I'll keep it short."#,
        r#"Oh sure, I can set that timer for you."#,
        r#"Oh sure thing, I will do that now."#,
        r#"You can do it from the settings page in two taps."#,
        r#"You can do it with a single command."#,
        r#"You got this wrong, the answer is seven."#,
        r#"It is a good job market for nurses right now."#,
        r#"The Fantastic Four is a film from 2005."#,
        r#"The Incredible Hulk is a comic book character."#,
        r#"The wow factor of the demo was low."#,
        r#"The WoW servers are down for maintenance tonight."#,
        r#"Hello Kitty is a brand from Japan."#,
        r#"Thanks to the heavy rain, the road is flooded."#,
        r#"Congratulations are not in order here."#,
        r#"It hasn't been wonderful lately."#,
        r#"It hadn't been fantastic until now."#,
        r#"None of it was wonderful."#,
        r#"The review was far from fantastic."#,
        r#"It was less than excellent, to be honest."#,
        r#"It is anything but awesome."#,
        r#"Was that a good idea?"#,
        r#"Would that be fantastic?"#,
        r#"If something went wrong, I would tell you right away."#,
        r#"It could be wonderful if it worked."#,
        r#"Good morning, but unfortunately the backup failed overnight."#,
        r#"Hi, I'm sorry, the file is gone."#,
        r#"Great news, but I can't save the changes."#,
        r#"I told you so little about it that you may be lost."#,
        r#"If you say so, I will start the timer."#,
        r#"Thanks for nothing."#,
        r#"I'm here for the weather report, nothing else."#,
        r#"I can't remember the last time I laughed this much."#,
        r#"Hey, careful, that pot is hot."#,
        r#"Well done, but the report still has three errors."#,
        r#"I appreciate that this is a complicated format."#,
        r#"Take your time machine for a spin."#,
        r#"Wow Air is a former airline."#,
        r#"Tell me more about the loss of your dog."#,
        r#"Awesome is defined as inspiring awe."#,
        r#"Hello world is the classic first program."#,
    ];

    /// Lines from the Python reviewer's adversarial files that the old reader
    /// still answered (owner-side phrasing, but the same classes: complaint tails,
    /// contrast, negation, noun uses of praise words).
    const PYTHON_REVIEW_SILENT: &[&str] = &[
        r#"thanks but no thanks"#,
        r#"thanks for nothing"#,
        r#"thank you for the update, but that was wrong"#,
        r#"thanks, that is not what I asked"#,
        r#"thanks to the new model it crashes"#,
        r#"hello world program in python"#,
        r#"hi tech companies are hiring"#,
        r#"wow that is a terrible idea"#,
        r#"lol you are so wrong"#,
        r#"haha not funny"#,
        r#"that's funny but also sad"#,
        r#"wow, I am so angry right now"#,
        r#"great job pip, you broke it"#,
        r#"nice work breaking everything"#,
        r#"i'm sad lol"#,
        r#"I'm sad. Just kidding"#,
        r#"sorry pip but you are wrong"#,
        r#"I failed the test lol"#,
        r#"thank you for nothing pip"#,
        r#"thanks, that did not help"#,
        r#"thanks pip, but I already knew that"#,
        r#"thank you, although it took forever"#,
        r#"thanks, still wrong"#,
        r#"good morning and I hate mondays"#,
        r#"hello, you are not working again"#,
        r#"hi, why are you so slow"#,
        r#"wow, you are so slow today"#,
        r#"wow you really messed that up"#,
        r#"wow pip that was a mistake"#,
        r#"haha you got it wrong again"#,
        r#"lol that was a bad answer"#,
        r#"hahaha you are useless"#,
        r#"that's amazing, said nobody"#,
        r#"I'm kidding, you are terrible"#,
        r#"good job pip, you deleted my file"#,
        r#"well done, you broke the timer"#,
        r#"oh my god pip you are useless"#,
        r#"oh my god, I'm so angry"#,
        r#"I can't believe it, you got it wrong again"#,
        r#"that's incredible, I guess"#,
        r#"it's awesome that you broke my timer"#,
        r#"that's amazing news, I lost the game though"#,
        r#"that's fantastic, not"#,
        r#"good job, pip, but that was the wrong song"#,
        r#"nice work, now it is worse"#,
        r#"hilarious, you got it wrong"#,
        r#"that's hilarious, you crashed"#,
        r#"that's funny, you forgot my name"#,
        r#"hello pip you are late"#,
        r#"hello, are you broken"#,
        r#"hi pip stop talking"#,
        r#"thank you pip for being so slow"#,
        r#"thanks for scaring me half to death"#,
        r#"good job. you deleted my alarm"#,
        r#"Thank you. That was not what I asked."#,
        r#"wow. that is a terrible idea."#,
        r#"wow. you are so slow."#,
        r#"hello darkness my old friend"#,
        r#"thanks to you I am in trouble"#,
        r#"I appreciate it when you stay quiet"#,
        r#"that's awesome, said nobody"#,
        r#"good job pip, good job, you broke the toaster"#,
        r#"I'm sorry pip but the answer was wrong"#,
        r#"Wow, you broke it"#,
    ];

    #[test]
    fn every_reviewer_line_is_silent() {
        for s in REVIEW_SILENT {
            assert_eq!(read_clause(s), None, "{s}");
        }
        for s in PYTHON_REVIEW_SILENT {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // the six controls of the round-2 review keep their answers
        assert_eq!(read("Oh no, the build failed."), Some(Family::Worry));
        assert_eq!(read("I'm sorry for your loss."), Some(Family::Worry));
        assert_eq!(read("Thank you so much for waiting."), Some(Family::Thanks));
        assert_eq!(read("Congratulations on the new job!"), Some(Family::Warm));
        assert_eq!(read_clause("There is no way to recover it."), None);
        assert_eq!(read_clause("Ha Long Bay is in Vietnam."), None);
    }

    // -- the generated check (item 10) --

    /// Every word combination of a phrase (alternatives per slot), capped.
    fn expand(slots: &Slots) -> Vec<String> {
        let mut out = vec![String::new()];
        for alts in slots {
            let mut next = Vec::new();
            for base in &out {
                for a in alts {
                    next.push(if base.is_empty() { a.clone() } else { format!("{base} {a}") });
                }
            }
            next.truncate(256);
            out = next;
        }
        out
    }

    fn cap(s: &str) -> String {
        let mut c = s.chars();
        c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
    }

    const CONTRAST_TAILS: &[&str] =
        &[", but the backup failed.", ". However, I can't save it.", ", although it is late.", ", yet it is gone.", ", though that is odd."];
    const COMPLAINT_TAILS: &[&str] = &[
        ", sadly the backup failed.",
        " and the file is gone.",
        ". The build failed.",
        ", it is not working.",
        " and you are so slow.",
        ", nothing else.",
    ];
    const QUESTION_PREFIXES: &[&str] = &["Do you think ", "Is it ", "Why ", "Was that ", "Can you tell me "];
    const CONDITIONAL_PREFIXES: &[&str] = &["If ", "Would ", "Could ", "Might ", "Whether ", "In case "];
    const NEGATION_PREFIXES: &[&str] = &[
        "It is not ",
        "I don't think ",
        "That was never ",
        "It hasn't been ",
        "It hadn't been ",
        "None of it was ",
        "It is far from ",
        "It is anything but ",
        "It was less than ",
        "It is unable to be ",
        "It is unlikely to be ",
        "It rarely is ",
        "It could be ",
        "It might be ",
        "That may be ",
        "It should be ",
        "Unless it is ",
        "We mustn't call that ",
        "You needn't be ",
        "Hardly ",
    ];

    #[test]
    fn generated_positive_rows_go_silent_under_contrast_questions_conditionals_and_negation() {
        let table = t();
        let mut examples = 0usize;
        let mut checks = 0usize;
        for row in &table.lexicon {
            if row.polarity < 0 {
                continue;
            }
            for slots in &row.phrases {
                for ex in expand(slots) {
                    let base = format!("{}!", cap(&ex));
                    let read = table.read_clause_rule(&base);
                    assert!(read.is_some(), "{}: the plain example {base:?} must read", row.rule);
                    examples += 1;
                    let mut variants: Vec<String> = Vec::new();
                    for tail in CONTRAST_TAILS {
                        variants.push(format!("{}{tail}", cap(&ex)));
                    }
                    if row.cautions {
                        for tail in COMPLAINT_TAILS {
                            variants.push(format!("{}{tail}", cap(&ex)));
                        }
                    }
                    for pre in QUESTION_PREFIXES {
                        variants.push(format!("{pre}{ex}?"));
                    }
                    for pre in CONDITIONAL_PREFIXES.iter().chain(NEGATION_PREFIXES) {
                        variants.push(format!("{pre}{ex}."));
                    }
                    for v in variants {
                        assert_eq!(table.read_clause_rule(&v), None, "{}: {v:?} must stay silent", row.rule);
                        checks += 1;
                    }
                }
            }
        }
        assert!(examples >= 100, "{examples} examples: the generator must not be vacuous");
        assert!(checks >= 3000, "{checks} checks");
    }

    #[test]
    fn generated_negative_rows_refuse_contrast_conditionals_and_negation_too() {
        let table = t();
        let mut examples = 0usize;
        for row in &table.lexicon {
            if row.polarity >= 0 {
                continue;
            }
            for slots in &row.phrases {
                for ex in expand(slots) {
                    let base = format!("{}!", cap(&ex));
                    assert!(table.read_clause_rule(&base).is_some(), "{}: {base:?} must read", row.rule);
                    examples += 1;
                    for tail in CONTRAST_TAILS {
                        let v = format!("{}{tail}", cap(&ex));
                        assert_eq!(table.read_clause_rule(&v), None, "{}: {v:?}", row.rule);
                    }
                    for pre in CONDITIONAL_PREFIXES.iter().chain(NEGATION_PREFIXES) {
                        let v = format!("{pre}{ex}.");
                        assert_eq!(table.read_clause_rule(&v), None, "{}: {v:?}", row.rule);
                    }
                }
            }
        }
        assert!(examples >= 40, "{examples}");
    }

    #[test]
    fn rows_do_not_contain_their_own_vetoes() {
        let table = t();
        for row in &table.lexicon {
            for slots in &row.phrases {
                for alts in slots {
                    for w in alts {
                        assert!(!table.contrast.iter().any(|c| c.as_slice() == w.as_bytes()), "{}: {w} is a contrast word", row.rule);
                        if row.polarity >= 0 && row.cautions {
                            assert!(!table.cautions.contains(w.as_bytes()), "{}: {w} is a caution word", row.rule);
                        }
                    }
                }
            }
        }
        // a drive-free sanity check on the lists themselves
        for w in ["but", "however", "though", "although", "yet"] {
            assert!(table.contrast.iter().any(|c| c.as_slice() == w.as_bytes()), "{w}");
        }
    }

    // -- (1) questions and conditionals --

    #[test]
    fn questions_and_conditionals_refuse_positive_matches_only_where_they_are_hypothetical() {
        for s in [
            "Was that a good idea?",
            "Do you want the good news or the bad news first?",
            "Do you want the good news, or the bad news first?",
            "Would that be fantastic?",
            "What if the disk fails tomorrow?",
            "Is that hilarious?",
            "Could it be wonderful if it worked?",
            "If this is good news, I will tell you.",
            "Whether it is great news or not, I cannot say.",
            "In case of good news, I will call.",
            "It could be wonderful if it worked.",
            "If something went wrong, I would tell you right away.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // a question after the reaction does not take the reaction back
        assert_eq!(read("Hello, how are you?"), Some(Family::Warm));
        assert_eq!(read("Thank you, would you like some tea?"), Some(Family::Thanks));
        assert_eq!(read("Wow, did you do that?"), Some(Family::Surprise));
        assert_eq!(read("Great news. Do you want to hear it?"), Some(Family::Warm));
        // a negative reading still stands in a question (the rule is for positive families)
        assert_eq!(read("Sorry to hear that, how are you feeling?"), Some(Family::Worry));
        assert_eq!(read("Oh no, did it fail?"), Some(Family::Worry));
    }

    // -- (2) all matches, contrast, polarity --

    #[test]
    fn all_matches_are_collected_and_mixed_clauses_say_nothing() {
        // a contrast word splits the clause
        for s in [
            "Good morning, but unfortunately the backup failed overnight.",
            "Great news, but I can't save the changes.",
            "Well done, however the report is late.",
            "Haha, though it was not funny.",
            "Thanks, although it took long.",
            "Wow, yet nobody came.",
            "I couldn't find it, but I found a copy.",
            "I'm sorry, but I can't do that.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // two matches of different polarity say nothing
        assert_eq!(read_clause("Oh my goodness, that's a shame."), None);
        assert_eq!(read_clause("Haha, sorry to hear that."), None);
        assert_eq!(read_clause("That's wonderful, I'm worried."), None);
        // a negative match beats a weak greeting, and so does any other content
        assert_eq!(read("Hello, unfortunately that is late."), Some(Family::Worry));
        let (c, rule) = t().read_clause_rule("Hi, good news!").unwrap();
        assert_eq!((c.family, rule), (Family::Warm, "warm-good-news"));
        let (_, rule) = t().read_clause_rule("Hello there.").unwrap();
        assert_eq!(rule, "warm-greeting");
        // two matches of the same polarity still pick the strongest
        let (c, rule) = t().read_clause_rule("Thank you, I'm glad.").unwrap();
        assert_eq!((c.family, rule), (Family::Thanks, "thanks-start"));
        let (c, rule) = t().read_clause_rule("Thank you, I'm so glad.").unwrap();
        assert_eq!((c.family, rule), (Family::Warm, "warm-im-glad"), "the longer pattern wins");
        // a clause that matches more than MAX_HITS times is noise
        let noisy = "haha ".repeat(MAX_HITS + 1);
        assert_eq!(read_clause(&noisy), None);
        assert_eq!(read(&"haha ".repeat(MAX_HITS)), Some(Family::Humour));
        // negative polarity: cautions and hedges do not silence a sad reading
        assert_eq!(read("Oh no, the file is gone."), Some(Family::Worry));
        assert_eq!(read("Unfortunately the backup failed."), Some(Family::Worry));
    }

    #[test]
    fn greetings_need_a_break_and_never_name_things() {
        for s in [
            "Hello Kitty is a brand.",
            "Hello world is the classic first program.",
            "Hi tech companies are hiring.",
            "Hey, careful, that pot is hot.",
            "Hi, I'm sorry, the file is gone.",
            "Good morning, you are late.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        for s in ["Hi!", "Hello there.", "Good morning, Richard.", "Hi, I found it.", "Welcome back!", "Nice to meet you."] {
            assert_eq!(read(s), Some(Family::Warm), "{s}");
        }
    }

    // -- (3) sarcasm rows are gone --

    #[test]
    fn sarcasm_rows_are_removed_until_there_is_a_measured_floor() {
        for s in [
            "Oh sure, I can set that timer for you.",
            "Oh sure thing, I will do that now.",
            "If you say so, I will start the timer.",
            "Yeah right, let me do that.",
            "Yeah, right, I will do it.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        for row in &t().lexicon {
            assert_ne!(row.rule, "doubt-if-you-say-so");
            for p in &row.phrases {
                let flat: Vec<String> = expand(p);
                for f in flat {
                    assert!(!["oh sure", "yeah right", "if you say so"].contains(&f.as_str()), "{}", row.rule);
                }
            }
        }
        // the honest doubt rows stay
        assert_eq!(read("I doubt that."), Some(Family::Doubt));
        assert_eq!(read("I'm skeptical."), Some(Family::Doubt));
        assert_eq!(read("I told you so."), Some(Family::Doubt));
        assert_eq!(read_clause("I told you so little about it that you may be lost."), None);
    }

    // -- (4) thanks and kind --

    #[test]
    fn thanks_and_kind_rows_need_their_full_forms() {
        for s in [
            "That's kind of a long story, and I'll keep it short.",
            "It is kind of cold.",
            "Thanks to the heavy rain, the road is flooded.",
            "Thank you to everyone.",
            "Thanks for nothing.",
            "Thank you for nothing, Pip.",
            "I appreciate that this is a complicated format.",
            "I appreciate it when you stay quiet.",
            "That's kind of you.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        for s in [
            "That's very kind of you.",
            "That's so kind of you to say.",
            "That is very kind of you.",
            "I appreciate your help.",
            "I really appreciate it.",
            "I appreciate that you told me.",
            "Thanks for waiting.",
            "Thanks, that helps.",
        ] {
            assert_eq!(read(s), Some(Family::Thanks), "{s}");
        }
        assert_eq!(strength_of("That's so kind of you."), Some(0.7));
    }

    // -- (5) negators and guards --

    #[test]
    fn negators_and_phrase_guards_are_extended() {
        for w in [
            "hasn't", "hadn't", "mustn't", "needn't", "none", "unable", "unlikely", "rarely", "seldom", "scarcely", "fail",
            "fails",
        ] {
            assert!(NEGATORS.contains(&w.as_bytes()), "{w}");
        }
        for g in [&["far", "from"][..], &["less", "than"], &["anything", "but"], &["anything", "other", "than"]] {
            assert!(GUARDS.iter().any(|x| x.iter().copied().eq(g.iter().map(|s| s.as_bytes()))), "{g:?}");
        }
        for s in [
            "It hasn't been wonderful lately.",
            "It hadn't been fantastic until now.",
            "None of it was wonderful.",
            "It is unable to be wonderful.",
            "It seems unlikely to be wonderful.",
            "It is rarely wonderful.",
            "It is seldom wonderful.",
            "It is scarcely wonderful.",
            "It fails to be wonderful.",
            "We mustn't call that excellent.",
            "The review was far from fantastic.",
            "It was less than excellent, to be honest.",
            "It is anything but awesome.",
            "It is anything other than awesome.",
            "It is far from being awesome.",
            "Congratulations are not in order here.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        // the guards do not cross a sentence
        assert_eq!(read("It was far. That's wonderful!"), Some(Family::Warm));
    }

    // -- (6) praise words, wow, support rows --

    #[test]
    fn praise_words_and_wow_need_the_clause_start_or_a_demonstrative() {
        for s in [
            "The Fantastic Four is a film from 2005.",
            "Awesome is defined as inspiring awe.",
            "The wow factor of the demo was low.",
            "The WoW servers are down tonight.",
            "Wow Air is a former airline.",
            "The Incredible Hulk is a comic book character.",
            "It is an unbelievable story.",
            "That was an excellent meal.",
            "Here is a fascinating fact.",
            "The word wonderful has three syllables.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        for (s, f) in [
            ("Wonderful!", Family::Warm),
            ("Awesome, thanks.", Family::Warm),
            ("That's wonderful.", Family::Warm),
            ("It is fantastic.", Family::Warm),
            ("Wow!", Family::Surprise),
            ("Wow, that is a lot.", Family::Surprise),
            ("Wow\u{2014}that is a lot.", Family::Surprise),
            ("Whoa, that is loud.", Family::Surprise),
            ("Incredible!", Family::Surprise),
            ("That's incredible.", Family::Surprise),
            ("Unbelievable, it worked.", Family::Surprise),
            ("Interesting!", Family::Curious),
            ("That's fascinating.", Family::Curious),
            ("Hilarious!", Family::Humour),
            ("That was hilarious.", Family::Humour),
        ] {
            assert_eq!(read(s), Some(f), "{s}");
        }
        // none of these words is an anywhere row any more
        for row in &t().lexicon {
            for p in &row.phrases {
                if p.len() == 1 && row.kind != Kind::Start {
                    for alt in &p[0] {
                        assert!(
                            !["incredible", "unbelievable", "wow", "wonderful", "fantastic", "awesome", "excellent", "brilliant", "terrific", "interesting", "hilarious"]
                                .contains(&alt.as_str()),
                            "{}: {alt} must be a start or demonstrative row",
                            row.rule
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn support_rows_are_start_rows_with_a_break_after() {
        for s in [
            "You can do it from the settings page in two taps.",
            "You can do it with a single command.",
            "You got this wrong, the answer is seven.",
            "It is a good job market for nurses right now.",
            "If you can do it, great.",
            "Good job market for nurses.",
            "Take your time machine for a spin.",
            "I'm here for the weather report, nothing else.",
            "It's okay to use a pencil.",
            "Great job on the list.",
        ] {
            assert_eq!(read_clause(s), None, "{s}");
        }
        for s in [
            "You can do it!",
            "You can do it, Richard.",
            "You've got this.",
            "You got this!",
            "Nice work!",
            "Good job, Richard.",
            "Great job.",
            "Take your time.",
            "I'm here for you.",
            "It's okay.",
        ] {
            assert_eq!(read(s), Some(Family::Support), "{s}");
        }
        for row in &t().lexicon {
            if row.family == Family::Support && matches!(row.rule.as_str(), "support-you-can" | "support-praise") {
                assert_eq!(row.kind, Kind::Start, "{}", row.rule);
                assert!(row.after_break, "{}", row.rule);
            }
        }
        // comforting rows answer bad news, so cautions and negators do not silence them
        assert_eq!(read("Don't worry, the file isn't lost."), Some(Family::Support));
        assert_eq!(read("It's okay, nothing is broken."), Some(Family::Support));
        assert_eq!(read_clause("Well done, but the report still has three errors."), None);
    }

    #[test]
    fn rows_with_an_object_need_it_short() {
        assert_eq!(read_clause("I can't remember the last time I laughed this much."), None);
        assert_eq!(read_clause("I couldn't find the file you asked me about earlier today."), None);
        assert_eq!(read("I couldn't find the right file."), Some(Family::Worry));
        assert_eq!(read("Something went wrong."), Some(Family::Worry));
        assert_eq!(read_clause("If something went wrong, I would tell you."), None);
    }

    // -- (7) the lone tag --

    #[test]
    fn a_lone_tag_is_capped_at_the_face_cue_and_never_startles() {
        let table = t();
        for name in ["joy", "curiosity", "skepticism", "concern"] {
            let tag = table.tag_cue(name).unwrap();
            let c = table.choose(&[(Source::Tag, tag)], &day()).unwrap();
            assert!(c.intensity <= FACE_CUE_INTENSITY + 1e-9, "{name}: {}", c.intensity);
            assert_eq!(c.source, Source::Tag);
            // the face already shows it: the wiring emits nothing
            assert!(!table.adds_to_tag(&c, Some(tag)), "{name}");
            // a drive nudge cannot lift it either
            let nudged = table.choose(&[(Source::Tag, tag), (Source::Drive, cue(tag.family, 1.0))], &day()).unwrap();
            assert!(nudged.intensity <= FACE_CUE_INTENSITY + 1e-9);
        }
        // a lone surprise tag never selects the startle rung
        let surprise = table.tag_cue("surprise").unwrap();
        assert_eq!(table.choose(&[(Source::Tag, surprise)], &day()), None);
        assert_eq!(table.choose(&[(Source::Tag, surprise), (Source::Drive, cue(Family::Surprise, 1.0))], &day()), None);
        // but real words in the same family still can
        let c = table.choose(&[(Source::Tag, surprise), (Source::Reply, cue(Family::Surprise, 0.4))], &day()).unwrap();
        assert_eq!((c.name, c.source), ("surprise", Source::Reply));
        assert!(c.intensity >= 0.55);
        // agreeing words raise a tag-family choice above the cap: that adds intensity
        let joy = table.tag_cue("joy").unwrap();
        let both = table.choose(&[(Source::Tag, joy), (Source::Reply, cue(Family::Warm, 0.7))], &day()).unwrap();
        assert!(both.intensity > FACE_CUE_INTENSITY);
        assert!(table.adds_to_tag(&both, Some(joy)));
    }

    #[test]
    fn the_director_emits_only_when_it_differs_from_the_tag_or_adds_intensity() {
        let table = t();
        let joy = table.tag_cue("joy").unwrap();
        let giggle = table.choose(&[(Source::Reply, cue(Family::Humour, 0.65))], &day()).unwrap();
        assert!(table.adds_to_tag(&giggle, Some(joy)), "another family");
        assert!(table.adds_to_tag(&giggle, None), "no tag at all");
        assert!(table.adds_to_tag(&giggle, Some(cue(Family::Calm, 1.0))), "a neutral tag shows nothing");
        let same = Choice { name: "pleased", intensity: FACE_CUE_INTENSITY, hold: 8.0, family: Family::Warm, rung: 0, source: Source::Tag };
        assert!(!table.adds_to_tag(&same, Some(joy)), "the same family at the face's own level");
        let more = Choice { intensity: 0.7, source: Source::Reply, ..same.clone() };
        assert!(table.adds_to_tag(&more, Some(joy)), "the same family with more intensity");
        let less = Choice { intensity: 0.4, source: Source::Reply, ..same.clone() };
        assert!(!table.adds_to_tag(&less, Some(joy)));
        assert!(adds_to_tag(&giggle, Some(joy)));
    }

    // -- (8) wire minimum --

    #[test]
    fn wire_choice_has_a_per_family_minimum_strength() {
        let table = t();
        assert_eq!(table.wire_min(Family::Surprise), 0.3);
        for f in Family::ALL {
            if f != Family::Surprise {
                assert_eq!(table.wire_min(f), 0.0, "{}", f.id());
            }
        }
        // a faint surprise passes min_score (4 x 0.06 = 0.24) but not the family floor
        assert!(table.wire_choice("surprise", 0.06, None).is_none());
        assert!(table.wire_choice("surprise", 0.29, None).is_none());
        let c = table.wire_choice("surprise", 0.3, None).unwrap();
        assert_eq!(c.name, "surprise");
        assert!(c.intensity >= 0.55);
        // the same floor applies to a heard vote inside `choose`
        assert_eq!(table.choose(&[(Source::Heard, cue(Family::Surprise, 0.29))], &day()), None);
        assert!(table.choose(&[(Source::Heard, cue(Family::Surprise, 0.3))], &day()).is_some());
        // other families keep the plain min_score edge
        assert!(table.wire_choice("worry", 0.06, None).is_some());
        // reply words of the same strength are not held to the wire floor
        assert!(table.choose(&[(Source::Reply, cue(Family::Surprise, 0.1))], &day()).is_some());
        // a bad floor is refused by the loader
        let bad = TABLE_JSON.replace("\"wire_min\": 0.3", "\"wire_min\": 1.5");
        assert!(Table::parse(&bad).is_err());
    }

    // -- (9) the wiring contract --

    fn moment(phase: Phase, accepted_turn: bool) -> Moment {
        Moment { phase, accepted_turn }
    }

    #[test]
    fn wiring_contract_gate_phase_and_dwell_for_every_output() {
        let phases = [Phase::Idle, Phase::Listening, Phase::Thinking, Phase::Speaking];
        for bits in 0u32..512 {
            let b = |i: u32| bits & (1 << i) != 0;
            let ctx = Ctx {
                asleep: b(0), waking: b(1), drowsy: b(2), muted: b(3), unwell: b(4),
                lid_blocked: b(5), guest: b(6), announcing: b(7), quiet: b(8), effects_on: false,
            };
            for phase in phases {
                for accepted in [false, true] {
                    for out in all_outputs() {
                        let mut dwell = Dwell::new();
                        let at = moment(phase, accepted);
                        let result = emit(&ctx, at, out, "pleased", &mut dwell, 10.0);
                        let gate_ok = gate(&ctx, out).is_ok();
                        let phase_ok = match out {
                            Output::Word => accepted && phase == Phase::Thinking,
                            Output::Reply => accepted && matches!(phase, Phase::Thinking | Phase::Speaking),
                            _ => true,
                        };
                        match result {
                            Ok(()) => {
                                assert!(gate_ok && phase_ok, "{bits:09b} {phase:?} {accepted} {out:?}");
                                assert_eq!(dwell.changes_this_turn(), 1, "an output is recorded in the dwell");
                            }
                            Err(Refusal::Veto(v)) => {
                                assert_eq!(gate(&ctx, out), Err(v));
                                assert_eq!(dwell.changes_this_turn(), 0, "a refusal records nothing");
                            }
                            Err(Refusal::Phase) => {
                                assert!(gate_ok && !phase_ok);
                                assert_eq!(dwell.changes_this_turn(), 0);
                            }
                            Err(Refusal::Pacing) => panic!("a fresh dwell cannot refuse"),
                        }
                        // the contract in words
                        if matches!(out, Output::Reply | Output::Word) && (!accepted || phase == Phase::Listening || phase == Phase::Idle) {
                            assert!(result.is_err(), "no word or reply cue without an accepted turn, or while listening or idle");
                        }
                        if out == Output::Reply && phase == Phase::Speaking && accepted && gate_ok {
                            assert_eq!(result, Ok(()));
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn wiring_contract_dwell_paces_every_output_and_refusals_cost_nothing() {
        let ctx = day();
        let mut d = Dwell::new();
        let think = moment(Phase::Thinking, true);
        assert_eq!(emit(&ctx, think, Output::Word, "concern", &mut d, 0.0), Ok(()));
        // a second change inside the gap is refused, whatever the output
        for out in [Output::Reply, Output::Job, Output::Spontaneous] {
            assert_eq!(emit(&ctx, think, out, "pleased", &mut d, 0.5), Err(Refusal::Pacing), "{out:?}");
        }
        assert_eq!(d.changes_this_turn(), 1);
        // a phase refusal does not use up the per-turn count
        assert_eq!(emit(&ctx, moment(Phase::Listening, true), Output::Reply, "pleased", &mut d, 5.0), Err(Refusal::Phase));
        assert_eq!(d.changes_this_turn(), 1);
        assert_eq!(emit(&ctx, moment(Phase::Speaking, true), Output::Reply, "pleased", &mut d, 5.0), Ok(()));
        assert_eq!(emit(&ctx, moment(Phase::Idle, false), Output::Job, "concern", &mut d, 7.0), Ok(()));
        // the per-turn cap of three holds for every output kind
        assert_eq!(emit(&ctx, think, Output::Reply, "curiosity", &mut d, 9.0), Err(Refusal::Pacing));
        // a startle pose cannot repeat inside ten seconds, from any output
        let mut d = Dwell::new();
        assert_eq!(emit(&ctx, think, Output::Reply, "surprise", &mut d, 0.0), Ok(()));
        assert_eq!(emit(&ctx, moment(Phase::Idle, false), Output::Job, "shocked", &mut d, 5.0), Err(Refusal::Pacing));
    }

    #[test]
    fn choose_is_callable_separately_for_the_heard_moment_and_for_reply_clauses() {
        let table = t();
        let joy = table.tag_cue("joy").unwrap();
        let sad = cue(Family::Worry, 0.5);
        // in one call, the joy tag wipes out the sad reaction to what was just said
        assert_eq!(table.choose(&[(Source::Tag, joy), (Source::Heard, sad)], &day()), None);
        // as two moments, the heard reaction stands and the tag is the face's business
        let heard = table.choose_heard(sad, &[], &day()).unwrap();
        assert_eq!((heard.name, heard.source, heard.hold), ("concern", Source::Heard, 1.2));
        let reply = table.choose_reply(None, Some(joy), &[], &day()).unwrap();
        assert_eq!(reply.family, Family::Warm);
        assert!(!table.adds_to_tag(&reply, Some(joy)));
        // reply clauses: words and tag; a heard vote cannot get in
        let c = table.choose_reply(Some(cue(Family::Humour, 0.65)), Some(joy), &[], &day()).unwrap();
        assert_eq!((c.family, c.source), (Family::Humour, Source::Reply));
        assert!(table.adds_to_tag(&c, Some(joy)));
        assert_eq!(table.choose_reply(None, None, &[], &day()), None);
        // the gate still applies inside each call
        let guest = Ctx { guest: true, ..day() };
        assert_eq!(table.choose_heard(sad, &[], &guest), None);
        assert_eq!(table.choose_reply(Some(cue(Family::Warm, 0.9)), Some(joy), &[], &guest), None);
        // drives only nudge, in either moment
        let plain = table.choose_heard(cue(Family::Worry, 0.4), &[], &day()).unwrap();
        let nudged = table.choose_heard(cue(Family::Worry, 0.4), &[cue(Family::Worry, 1.0)], &day()).unwrap();
        assert!(nudged.intensity > plain.intensity);
        assert_eq!(table.choose_heard(cue(Family::Worry, 0.0), &[cue(Family::Worry, 1.0)], &day()), None);
        // the free functions agree
        assert_eq!(choose_heard(sad, &[], &day()), Some(heard));
    }

    #[test]
    fn new_row_attributes_are_validated() {
        let bad = TABLE_JSON.replace("\"opener\": true", "\"opener\": 1");
        assert!(Table::parse(&bad).is_err());
        let bad = TABLE_JSON.replace("\"max_rest\": 4", "\"max_rest\": \"four\"");
        assert!(Table::parse(&bad).is_err());
        let bad = TABLE_JSON.replace("\"refuse_next\": [\"to\", \"for nothing\"]", "\"refuse_next\": [\"To\"]");
        assert!(Table::parse(&bad).is_err());
        let bad = TABLE_JSON.replace("\"cautions\": false", "\"cautions\": \"no\"");
        assert!(Table::parse(&bad).is_err());
        let bad = TABLE_JSON.replace("\"contrast\": [\"but\",", "\"contrast\": [\"Bu t\",");
        assert!(Table::parse(&bad).is_err());
        // an opener must be a start row
        let bad = TABLE_JSON.replace(
            "{\"rule\": \"warm-greeting\", \"kind\": \"start\"",
            "{\"rule\": \"warm-greeting\", \"kind\": \"phrase\"",
        );
        assert!(Table::parse(&bad).is_err());
    }

    // -- recall (what the structural rules cost) --

    #[test]
    fn plain_sincere_replies_still_read() {
        for (s, f) in [
            ("Thank you for telling me.", Family::Thanks),
            ("Thanks for letting me know!", Family::Thanks),
            ("Thank you so much for waiting.", Family::Thanks),
            ("I really appreciate your help.", Family::Thanks),
            ("That's very kind of you.", Family::Thanks),
            ("I'm sorry about that.", Family::Apology),
            ("Sorry about the wait.", Family::Apology),
            ("My mistake, I read the wrong line.", Family::Apology),
            ("Don't worry about it.", Family::Support),
            ("No worries!", Family::Support),
            ("Take your time, I will wait.", Family::Support),
            ("You can do it!", Family::Support),
            ("Nice work!", Family::Support),
            ("Well done!", Family::Support),
            ("Glad to hear that.", Family::Warm),
            ("I'm glad you like it.", Family::Warm),
            ("Happy to help!", Family::Warm),
            ("You're welcome.", Family::Warm),
            ("Congratulations on the new job.", Family::Warm),
            ("Yay, it worked.", Family::Warm),
            ("Wonderful, thank you.", Family::Warm),
            ("That's great!", Family::Warm),
            ("Great news, it saved.", Family::Warm),
            ("Good idea, let me do that.", Family::Warm),
            ("Hello there.", Family::Warm),
            ("Good morning, Richard.", Family::Warm),
            ("Haha, good one.", Family::Humour),
            ("Hahaha, that's a good one.", Family::Humour),
            ("Just kidding.", Family::Humour),
            ("That's funny!", Family::Humour),
            ("Wow, that is a lot of boxes.", Family::Surprise),
            ("Whoa, that is loud.", Family::Surprise),
            ("No way!", Family::Surprise),
            ("Oh my goodness.", Family::Surprise),
            ("I can't believe it worked.", Family::Surprise),
            ("Interesting!", Family::Curious),
            ("Tell me more.", Family::Curious),
            ("Good question.", Family::Curious),
            ("I wonder what it is.", Family::Curious),
            ("I have an idea, let's try it.", Family::Curious),
            ("I doubt that.", Family::Doubt),
            ("Oh no, the file is gone.", Family::Worry),
            ("I couldn't find it.", Family::Worry),
            ("Unfortunately the backup failed.", Family::Worry),
            ("Sorry to hear that.", Family::Worry),
            ("That's a shame.", Family::Worry),
            ("Something went wrong.", Family::Worry),
        ] {
            assert_eq!(read(s), Some(f), "{s}");
        }
    }

    /// Former positives that are silent now, kept as a list so the cost is
    /// visible and a change shows up here. Each is silent on purpose.
    #[test]
    fn recall_cost_is_pinned() {
        for (s, why) in [
            ("Thanks for waiting, I'm sorry it took so long.", "caution word"),
            ("Thanks, no problem at all.", "caution word and negator"),
            ("That's kind of you.", "only the very and so forms"),
            ("I'm sorry, but I can't do that.", "contrast word"),
            ("Great job on the list.", "praise needs a break after"),
            ("Glad to hear it, I was worried.", "caution word"),
            ("That's wonderful news.", "demonstrative praise needs a break after"),
            ("Nice to see you again.", "greetings need a break after"),
            ("What if we try the other one?", "a question"),
            ("Wow, I didn't know that.", "a negator in the clause"),
            ("Interesting, I did not know that.", "a negator in the clause"),
            ("Thanks, I couldn't have done it without you.", "a negator in the clause"),
            ("I'm glad you didn't give up.", "a negator in the clause"),
            ("Haha, I can't stop laughing.", "a negator in the clause"),
            ("Happy to help, don't hesitate to ask.", "a negator in the clause"),
            ("That sounds wonderful.", "praise needs a demonstrative"),
            ("Wow that is a lot.", "wow needs a break after"),
        ] {
            assert_eq!(read_clause(s), None, "{s} ({why})");
        }
    }

    // -- cost (host) -------------------------------------------------------

    #[test]
    #[ignore = "micro-benchmark: cargo test --release director::tests::bench -- --ignored --nocapture"]
    fn bench_read_clause_28_words() {
        use std::hint::black_box;
        use std::time::Instant;
        let table = t();
        // synthetic, 28 words; the first has no trigger (full scan of every row)
        let miss = "Your list has twelve items and the third one is a long entry about the garden path that leads to the old shed behind the old house today";
        let hit = "Well the garden shed has twelve boxes and the third box holds a very old lamp so the dust there is a lot to clean up, haha, really";
        assert_eq!(miss.split_whitespace().count(), 28);
        assert_eq!(hit.split_whitespace().count(), 28);
        for (label, s) in [("no match", miss), ("late match", hit)] {
            let words = s.split_whitespace().count();
            // size the run from a probe so a slow build still finishes
            let probe = Instant::now();
            for _ in 0..200 {
                black_box(table.read_clause(black_box(s)));
            }
            let per = probe.elapsed().as_nanos().max(1) as f64 / 200.0;
            let n = ((300_000_000.0 / per) as u32).clamp(200, 200_000);
            let start = Instant::now();
            let mut hits = 0u32;
            for _ in 0..n {
                if black_box(table.read_clause(black_box(s))).is_some() {
                    hits += 1;
                }
            }
            let ns = start.elapsed().as_nanos() as f64 / n as f64;
            println!("host: read_clause {label}, {words} words, {ns:.0} ns per call (hits {hits}/{n})");
        }
        let votes = [
            (Source::Reply, cue(Family::Humour, 0.65)),
            (Source::Tag, cue(Family::Warm, 0.55)),
            (Source::Drive, cue(Family::Warm, 0.3)),
        ];
        let n = 1_000_000u32;
        let start = Instant::now();
        for _ in 0..n {
            black_box(table.choose(black_box(&votes), black_box(&day())));
        }
        println!("host: choose, 3 votes, {:.0} ns per call", start.elapsed().as_nanos() as f64 / n as f64);
    }
}
