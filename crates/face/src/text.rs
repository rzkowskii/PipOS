//! A little text for the face's overlays, in being-console's own small face
//! (JetBrains Mono, 8x16, SIL Open Font License 1.1; the licence travels in
//! the header this reads). The table is parsed only when text is first
//! needed, so a face that never shows words never pays for them.
//!
//! The table is the crate's own copy (`console-font-small.h`): the image
//! build compiles the face from a source tree without `os/`, so it cannot
//! read the console's header. A test keeps the copy in step with it.

use std::collections::HashMap;
use std::sync::OnceLock;

const HEADER: &str = include_str!("console-font-small.h");
const TABLE: &str = "console_font_small_regular[CONSOLE_FONT_GLYPHS] = {";

pub struct Font {
    pub w: usize,
    pub h: usize,
    glyphs: HashMap<char, Vec<u8>>,
}

/// Parse one face's table: a `/* WxH weight U+CODE */` comment, then H
/// quoted rows of W hex coverage digits.
pub fn parse(source: &str, table: &str) -> Result<Font, String> {
    let start = source.find(table).ok_or("font table not found")? + table.len();
    let end = source[start..].find("};").ok_or("font table never ends")? + start;
    let body = &source[start..end];
    let mut glyphs = HashMap::new();
    let (mut w, mut h) = (0usize, 0usize);
    for entry in body.split("/*").skip(1) {
        let (comment, rows) = entry.split_once("*/").ok_or("unclosed glyph comment")?;
        let mut words = comment.split_whitespace();
        let size = words.next().ok_or("glyph without a size")?;
        let (gw, gh) = size.split_once('x').ok_or("glyph size is not WxH")?;
        let (gw, gh): (usize, usize) = (gw.parse().map_err(|_| "bad glyph width")?, gh.parse().map_err(|_| "bad glyph height")?);
        let code = words.find_map(|word| word.strip_prefix("U+")).ok_or("glyph without a code point")?;
        let code = u32::from_str_radix(code, 16).map_err(|_| "bad code point")?;
        let ch = char::from_u32(code).ok_or("code point is not a character")?;
        let mut coverage = Vec::with_capacity(gw * gh);
        for row in rows.split('"').skip(1).step_by(2).take(gh) {
            if row.len() != gw {
                return Err(format!("U+{code:04X}: a row is {} wide, not {gw}", row.len()));
            }
            for digit in row.chars() {
                coverage.push(digit.to_digit(16).ok_or("coverage is not hex")? as u8 * 17);
            }
        }
        if coverage.len() != gw * gh {
            return Err(format!("U+{code:04X}: {} rows, not {gh}", coverage.len() / gw.max(1)));
        }
        (w, h) = (gw, gh);
        glyphs.insert(ch, coverage);
    }
    if glyphs.is_empty() {
        return Err("font table is empty".to_string());
    }
    Ok(Font { w, h, glyphs })
}

/// The small face, parsed once on first use.
pub fn font() -> Option<&'static Font> {
    static FONT: OnceLock<Option<Font>> = OnceLock::new();
    FONT.get_or_init(|| match parse(HEADER, TABLE) {
        Ok(font) => Some(font),
        Err(e) => {
            eprintln!("face: no overlay text: {e}");
            None
        }
    })
    .as_ref()
}

/// A premultiplied RGBA picture of some lines of text on a translucent
/// backing, top row first.
pub struct Picture {
    pub w: usize,
    pub h: usize,
    pub rgba: Vec<u8>,
}

/// Colour and backing of a block of text.
#[derive(Clone, Copy)]
pub struct Ink {
    pub rgb: [u8; 3],
    /// Opacity of the dark backing behind the text, 0-255.
    pub backing: u8,
}

impl Font {
    /// Wrap `text` to at most `columns` characters a line, breaking at spaces
    /// where it can and keeping a hanging indent of `indent` spaces.
    pub fn wrap(text: &str, columns: usize, indent: usize) -> Vec<String> {
        let columns = columns.max(indent + 8);
        let mut lines = Vec::new();
        let mut line = String::new();
        for word in text.split_whitespace() {
            let mut word: Vec<char> = word.chars().collect();
            loop {
                let used = line.chars().count();
                let room = columns.saturating_sub(used + usize::from(used > 0 && !line.ends_with(' ')));
                if word.len() <= room {
                    if used > 0 && !line.ends_with(' ') {
                        line.push(' ');
                    }
                    line.extend(word.iter());
                    break;
                }
                let bare = line.trim_end().is_empty() || line.chars().count() <= indent;
                if bare {
                    // A word longer than a whole line: cut it.
                    let take = columns.saturating_sub(line.chars().count()).max(1);
                    line.extend(word.drain(..take.min(word.len())));
                }
                lines.push(std::mem::take(&mut line));
                line = " ".repeat(indent);
                if word.is_empty() {
                    break;
                }
            }
        }
        if !line.trim().is_empty() || lines.is_empty() {
            lines.push(line);
        }
        lines
    }

    /// Draw lines into a picture `columns` wide, with a margin of half a cell.
    pub fn render(&self, lines: &[String], columns: usize, ink: Ink) -> Picture {
        let pad = self.w / 2 + 2;
        let (w, h) = (columns * self.w + 2 * pad, lines.len() * self.h + 2 * pad);
        let mut rgba = vec![0u8; w * h * 4];
        for px in rgba.chunks_exact_mut(4) {
            px[3] = ink.backing;
        }
        for (row, line) in lines.iter().enumerate() {
            for (col, ch) in line.chars().take(columns).enumerate() {
                let Some(glyph) = self.glyphs.get(&ch).or_else(|| self.glyphs.get(&'?')) else { continue };
                for gy in 0..self.h {
                    for gx in 0..self.w {
                        let a = glyph[gy * self.w + gx];
                        if a == 0 {
                            continue;
                        }
                        let (x, y) = (pad + col * self.w + gx, pad + row * self.h + gy);
                        let px = &mut rgba[(y * w + x) * 4..(y * w + x) * 4 + 4];
                        // Premultiplied: ink at its coverage over the dark
                        // backing (drawn with ONE, ONE_MINUS_SRC_ALPHA).
                        let a16 = u16::from(a);
                        for (out, ink) in px[..3].iter_mut().zip(ink.rgb) {
                            *out = (u16::from(ink) * a16 / 255) as u8;
                        }
                        px[3] = (a16 + u16::from(ink.backing) * (255 - a16) / 255) as u8;
                    }
                }
            }
        }
        Picture { w, h, rgba }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_console_face_parses_with_every_printable_ascii_letter() {
        let font = parse(HEADER, TABLE).unwrap();
        assert_eq!((font.w, font.h), (8, 16));
        for ch in ' '..='~' {
            assert!(font.glyphs.contains_key(&ch), "missing {ch:?}");
        }
        assert!(font.glyphs.contains_key(&'·') && font.glyphs.contains_key(&'●'));
        assert!(font.glyphs[&'A'].iter().any(|&a| a > 0));
        assert!(font.glyphs[&' '].iter().all(|&a| a == 0));
    }

    #[test]
    fn the_copy_matches_the_consoles_own_table() {
        let console = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../os/package/being/being-console-font.h");
        // A packaged source tree has no os/; there is nothing to compare then.
        let Ok(source) = std::fs::read_to_string(&console) else { return };
        let table = |text: &str| {
            let start = text.find(TABLE).expect("table");
            let end = text[start..].find("};").expect("end") + start;
            text[start..end].to_string()
        };
        assert!(table(&source) == table(HEADER), "regenerate src/console-font-small.h from {}", console.display());
    }

    #[test]
    fn a_broken_table_is_an_error_not_a_crash() {
        assert!(parse("nothing here", TABLE).is_err());
        let short = format!("{TABLE}\n /* 2x2 regular U+0041 */ \"ff\" \"f\",\n}};");
        assert!(parse(&short, TABLE).is_err());
    }

    #[test]
    fn wrapping_breaks_at_spaces_and_keeps_the_indent() {
        let lines = Font::wrap("heard: what is the weather going to be like tomorrow", 24, 7);
        assert_eq!(lines[0], "heard: what is the");
        assert!(lines[1..].iter().all(|l| l.starts_with("       ") && l.chars().count() <= 24), "{lines:?}");
        let long = Font::wrap("x: abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz", 20, 3);
        assert!(long.iter().all(|l| l.chars().count() <= 20), "{long:?}");
        assert_eq!(long.concat().replace(' ', "").len(), "x:".len() + 52);
        assert_eq!(Font::wrap("", 20, 0), vec![String::new()]);
    }

    #[test]
    fn rendered_text_is_lit_where_letters_are() {
        let font = parse(HEADER, TABLE).unwrap();
        let ink = Ink { rgb: [110, 210, 255], backing: 0 };
        let pic = font.render(&["I".to_string()], 1, ink);
        assert_eq!((pic.w, pic.h), (8 + 2 * 6, 16 + 2 * 6));
        let lit = pic.rgba.chunks_exact(4).filter(|px| px[3] > 0).count();
        assert!(lit > 4 && lit < 8 * 16);
    }
}
