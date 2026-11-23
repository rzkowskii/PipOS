//! being-bodycare: the one owner of every power and thermal write on the
//! laptop (IMPROVEMENT-PLAN task 2.8 and section 4).
//!
//! Fail-safe by construction: it only raises the fan *floor* (the SMC's own
//! curve stays in charge), only lowers CPU limits, journals each original
//! before changing it, and undoes everything on exit (`--restore`). It ships
//! in observe mode, which decides and reports but writes nothing.

pub mod care;
pub mod hw;
pub mod journal;
pub mod policy;
