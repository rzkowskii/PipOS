// Project the concept animation's eye channels onto Pip's shader uniforms.
(function () {
  'use strict';
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

  function packed(left, right, motion = {}) {
    const { x = 0, y = 0, roll = 0, gazeX = 0, gazeY = 0, breath = 1, blink = 0 } = motion;
    const cosine = Math.cos(roll), sine = Math.sin(roll);
    function project(eye) {
      const localX = eye.x + gazeX, localY = eye.y + gazeY;
      const w = clamp(eye.w, .02, 1.45), h = clamp(eye.h * breath, .02, .85);
      const projected = {
        x: clamp(cosine * localX - sine * localY + x, -1.48 + w, 1.48 - w),
        y: clamp(sine * localX + cosine * localY + y, -.88 + h, .88 - h),
        w, h, round: Math.max(2, eye.round), tilt: eye.tilt + roll,
        top: eye.top, top_slant: eye.topSlant, top_curve: eye.topCurve,
        bot: eye.bot, bot_slant: eye.botSlant, bot_curve: eye.botCurve,
        glow: clamp(eye.glow, 0, 1.3),
      };
      if (blink > 0) PipAnim.closeLids(projected, clamp(blink, 0, 1));
      return projected;
    }
    return PipAnim.uniforms({ l: project(left), r: project(right) });
  }

  const api = { packed };
  if (typeof window !== 'undefined') window.FaceLabDeviceEyes = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
