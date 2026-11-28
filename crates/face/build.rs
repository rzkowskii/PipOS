//! Turn the shared expression data into a checked table before the face builds.

use serde::de::DeserializeOwned;
use serde_json::{json, Map, Value};
use std::{
    collections::{HashMap, HashSet},
    env,
    fmt::Write,
    fs,
    path::PathBuf,
};

const CHANNELS: [&str; 13] = [
    "x",
    "y",
    "w",
    "h",
    "round",
    "tilt",
    "top",
    "top_slant",
    "top_curve",
    "bot",
    "bot_slant",
    "bot_curve",
    "glow",
];
const CONCEPT_EYES: [&str; 13] = [
    "x", "y", "w", "h", "round", "tilt", "top", "topSlant", "topCurve", "bot", "botSlant",
    "botCurve", "glow",
];
const BROWS: [&str; 7] = ["y", "dx", "len", "tilt", "curve", "th", "a"];
const MOUTH: [&str; 15] = [
    "x", "w", "y", "corner", "skew", "arc", "open", "round", "cat", "wave", "zig", "tongue", "low",
    "a", "talk",
];
const EFFECTS: [&str; 26] = [
    "sweat", "cry", "blush", "hearts", "sparkle", "zzz", "question", "exclaim", "anger", "steam",
    "notes", "bulb", "dots", "gloom", "shiver", "bounce", "sway", "breath", "snow", "flame",
    "glitch", "scan", "bubble", "check", "warn", "battery",
];
const DETAILS: [&str; 7] = [
    "sp_heart",
    "sp_x",
    "sp_spiral",
    "sp_star",
    "pup",
    "shine",
    "wet",
];
const KICK: [&str; 5] = ["pop", "recoil", "roll", "burst", "pupil"];
const TINT: [&str; 4] = ["ac", "acr", "acg", "acb"];

fn field<T: DeserializeOwned>(row: &mut Map<String, Value>, key: &str) -> T {
    serde_json::from_value(row.remove(key).unwrap_or(Value::Null))
        .unwrap_or_else(|err| panic!("invalid expression field {key}: {err}"))
}

fn variant(name: &str) -> String {
    assert!(
        name.as_bytes().first().is_some_and(u8::is_ascii_lowercase),
        "invalid expression name: {name}"
    );
    name.split('_')
        .map(|word| {
            assert!(
                !word.is_empty()
                    && word
                        .bytes()
                        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit()),
                "invalid expression name: {name}"
            );
            format!("{}{}", word[..1].to_ascii_uppercase(), &word[1..])
        })
        .collect()
}

#[derive(Clone, Copy)]
struct Concept {
    eyes: [[f32; CHANNELS.len()]; 2],
    brows: [[f32; BROWS.len()]; 2],
    mouth: [f32; MOUTH.len()],
    effects: [f32; EFFECTS.len()],
    details: [f32; DETAILS.len()],
    kick: [f32; KICK.len()],
    tint: [f32; TINT.len()],
}

fn check_channels<const N: usize>(schema: &mut Map<String, Value>, key: &str, expected: [&str; N]) {
    let names: Vec<String> = field(schema, key);
    assert!(
        names.iter().map(String::as_str).eq(expected),
        "incompatible Face Lab channels for {key}: {names:?}"
    );
}

fn load_concepts(
    poses: &mut Vec<Map<String, Value>>,
    native: &HashSet<String>,
) -> HashMap<String, Concept> {
    let data =
        fs::read_to_string("concept-expressions.json").expect("read concept-expressions.json");
    let mut catalogue: Map<String, Value> =
        serde_json::from_str(&data).expect("parse concept-expressions.json");
    let mut schema: Map<String, Value> = field(&mut catalogue, "channels");
    check_channels(&mut schema, "eye", CONCEPT_EYES);
    check_channels(&mut schema, "brow", BROWS);
    check_channels(&mut schema, "mouth", MOUTH);
    check_channels(&mut schema, "effects", EFFECTS);
    check_channels(&mut schema, "details", DETAILS);
    check_channels(&mut schema, "kick", KICK);
    check_channels(&mut schema, "tint", TINT);
    assert!(
        schema.is_empty(),
        "unknown Face Lab channel sets: {schema:?}"
    );
    let rows: Vec<Map<String, Value>> = field(&mut catalogue, "poses");
    assert!(
        catalogue.is_empty(),
        "unknown Face Lab catalogue fields: {catalogue:?}"
    );

    let mut concepts = HashMap::new();
    for mut row in rows {
        let name: String = field(&mut row, "name");
        let mut left: [f32; CHANNELS.len()] = field(&mut row, "left");
        let mut right: [f32; CHANNELS.len()] = field(&mut row, "right");
        let [gaze_x, gaze_y, roll]: [f32; 3] = field(&mut row, "motion");
        let concept = Concept {
            eyes: [left, right],
            brows: field(&mut row, "brows"),
            mouth: field(&mut row, "mouth"),
            effects: field(&mut row, "effects"),
            details: field(&mut row, "details"),
            kick: field(&mut row, "kick"),
            tint: field(&mut row, "tint"),
        };
        assert!(row.is_empty(), "unknown Face Lab fields in {name}: {row:?}");
        let mut visual_numbers = concept
            .eyes
            .iter()
            .flatten()
            .chain(concept.brows.iter().flatten())
            .chain(&concept.mouth)
            .chain(&concept.effects)
            .chain(&concept.details)
            .chain(&concept.kick);
        assert!(
            visual_numbers.all(|channel| channel.is_finite()),
            "invalid Face Lab channels for {name}"
        );
        assert!(
            concept
                .tint
                .iter()
                .all(|channel| (0.0..=1.0).contains(channel)),
            "invalid Face Lab tint for {name}"
        );
        assert!(
            concepts.insert(name.clone(), concept).is_none(),
            "duplicate Face Lab expression: {name}"
        );

        if !native.contains(&name) {
            // Three concepts use pointier contours than the native eye motion permits.
            left[4] = left[4].max(2.0);
            right[4] = right[4].max(2.0);
            let row = json!({
                "name": name,
                "left": left,
                "right": right,
                "audition": true,
                "motion": {
                    "gaze": [0.5 * gaze_x, 0.5 * gaze_y],
                    "roll": roll.clamp(-0.12, 0.12),
                    "breath": concept.effects[17].clamp(0.6, 1.6),
                },
            });
            poses.push(row.as_object().expect("Face Lab pose object").clone());
        }
    }
    concepts
}

fn named_struct(name: &str, fields: &[&str], values: &[f32]) -> String {
    let members: Vec<String> = fields
        .iter()
        .zip(values)
        .map(|(field, value)| format!("{field}: {value:?}"))
        .collect();
    format!("{name} {{ {} }}", members.join(", "))
}

fn write_visual(table: &mut String, name: &str, concepts: &HashMap<String, Concept>) {
    let source = match name {
        "speaking" => "attentive",
        "dreaming" => "asleep",
        _ => name,
    };
    let mut concept = *concepts
        .get(source)
        .unwrap_or_else(|| panic!("missing Face Lab visuals for {source}"));
    if name == "speaking" {
        concept.mouth[14] = 1.0;
    }
    let brow_fields = ["y", "dx", "len", "tilt", "curve", "th", "alpha"];
    let mut mouth_fields = MOUTH;
    mouth_fields[13] = "alpha";
    let detail_fields = ["heart", "cross", "spiral", "star", "pupil", "shine", "wet"];
    let [left, right] = concept
        .brows
        .map(|row| named_struct("Brow", &brow_fields, &row));
    let mouth = named_struct("Mouth", &mouth_fields, &concept.mouth);
    let details = named_struct("EyeDetails", &detail_fields, &concept.details);
    let kick = named_struct("VisualKick", &KICK, &concept.kick);
    let [amount, red, green, blue] = concept.tint;
    let eyes: Vec<_> = concept.eyes.into_iter().flatten().collect();
    writeln!(table,
        "        visual: Visual {{ eyes: {eyes:?}, brows: [{left}, {right}], mouth: {mouth}, effects: {:?}, details: {details}, kick: {kick}, tint: MoodTint {{ amount: {amount:?}, colour: [{red:?}, {green:?}, {blue:?}] }} }},",
        concept.effects,
    ).unwrap();
}

fn main() {
    println!("cargo:rerun-if-changed=expressions.json");
    println!("cargo:rerun-if-changed=concept-expressions.json");
    let data = fs::read_to_string("expressions.json").expect("read expressions.json");
    let mut catalogue: Map<String, Value> =
        serde_json::from_str(&data).expect("parse expressions.json");
    let channels: [String; CHANNELS.len()] = field(&mut catalogue, "channels");
    let default_damping: f32 = field(&mut catalogue, "default_damping");
    let mut poses: Vec<Map<String, Value>> = field(&mut catalogue, "poses");
    assert!(
        catalogue.is_empty(),
        "unknown expression catalogue fields: {catalogue:?}"
    );
    assert_eq!(
        channels, CHANNELS,
        "expression channels must match Eye::from_slice"
    );
    assert!(
        default_damping > 0.0 && default_damping < 1.0,
        "invalid default damping"
    );
    let native = poses
        .iter()
        .map(|row| {
            row["name"]
                .as_str()
                .expect("native expression name")
                .to_owned()
        })
        .collect();
    let concepts = load_concepts(&mut poses, &native);

    let mut seen = HashSet::new();
    let mut variants = String::new();
    let mut public = String::new();
    let mut auditions = String::new();
    let mut all = String::new();
    let mut table = String::new();
    for mut row in poses {
        let name: String = field(&mut row, "name");
        let left: [f32; CHANNELS.len()] = field(&mut row, "left");
        let right: [f32; CHANNELS.len()] = field(&mut row, "right");
        let private = field::<Option<bool>>(&mut row, "private").unwrap_or(false);
        let audition = field::<Option<bool>>(&mut row, "audition").unwrap_or(false);
        let concept_only = !native.contains(&name);
        let lively = field::<Option<bool>>(&mut row, "lively").unwrap_or(false);
        let damping = field::<Option<f32>>(&mut row, "damping").unwrap_or(default_damping);
        let mut motion =
            field::<Option<Map<String, Value>>>(&mut row, "motion").unwrap_or_default();
        let gaze = field::<Option<[f32; 2]>>(&mut motion, "gaze").unwrap_or([0.0; 2]);
        let roll = field::<Option<f32>>(&mut motion, "roll").unwrap_or(0.0);
        let blink_interval = field::<Option<f32>>(&mut motion, "blink_interval").unwrap_or(1.0);
        let breath = field::<Option<f32>>(&mut motion, "breath").unwrap_or(1.0);
        let kick: Option<String> = field(&mut motion, "kick");
        assert!(
            motion.is_empty(),
            "unknown motion fields in {name}: {motion:?}"
        );
        assert!(row.is_empty(), "unknown fields in {name}: {row:?}");

        let id = variant(&name);
        assert!(seen.insert(id.clone()), "duplicate expression: {name}");
        for eye in [left, right] {
            let [x, y, w, h, round, ..] = eye;
            assert!(
                eye.into_iter().all(f32::is_finite)
                    && w > 0.0
                    && h > 0.0
                    && round >= 2.0
                    && x.abs() + w < 1.5
                    && y.abs() + h < 0.9,
                "invalid eye geometry for {name}: {eye:?}"
            );
        }
        assert!(damping > 0.0 && damping < 1.0, "invalid damping for {name}");
        assert!(!(private && audition), "conflicting visibility for {name}");
        assert!(
            gaze.iter().all(|axis| (-0.5..=0.5).contains(axis))
                && (-0.12..=0.12).contains(&roll)
                && (0.6..=1.6).contains(&blink_interval)
                && (0.6..=1.6).contains(&breath),
            "invalid motion range for {name}"
        );
        let kick = match kick.as_deref().unwrap_or("none") {
            "none" => "None",
            "startle" => "Startle",
            "laugh" => "Laugh",
            "flinch" => "Flinch",
            "relief" => "Relief",
            unknown => panic!("unknown kick for {name}: {unknown}"),
        };

        writeln!(variants, "    {id},").unwrap();
        writeln!(all, "        Self::{id},").unwrap();
        if audition {
            writeln!(auditions, "        Self::{id},").unwrap();
        } else if !private {
            writeln!(public, "        Self::{id},").unwrap();
        }
        let eyes: Vec<_> = left.into_iter().chain(right).collect();
        writeln!(table, "    Expression {{ name: {name:?}, eyes: {eyes:?}, lively: {lively}, damping: {damping:?}, audition: {audition}, concept_only: {concept_only},").unwrap();
        writeln!(table, "        motion: Motion {{ gaze: {gaze:?}, roll: {roll:?}, blink_interval: {blink_interval:?}, breath: {breath:?}, kick: Kick::{kick} }},").unwrap();
        write_visual(&mut table, &name, &concepts);
        writeln!(table, "    }},").unwrap();
    }

    let generated = format!(
        "#[derive(Clone, Copy, Debug, PartialEq, Eq)]\n\
         pub enum ExprId {{\n{variants}}}\n\
         impl ExprId {{\n\
             pub const ALL: &'static [Self] = &[\n{all}    ];\n\
             pub const PUBLIC: &'static [Self] = &[\n{public}    ];\n\
             pub const AUDITIONS: &'static [Self] = &[\n{auditions}    ];\n\
         }}\n\
         static EXPRESSIONS: [Expression; ExprId::ALL.len()] = [\n{table}];\n\
         pub const DEFAULT_DAMPING: f32 = {default_damping:?};\n"
    );
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR"));
    fs::write(out.join("expressions.rs"), generated).expect("write expression table");
}
