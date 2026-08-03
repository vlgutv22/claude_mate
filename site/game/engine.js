// SHIP IT — the web edition.
//
// TWO ENGINES, ONE GAME. The device engine (firmware/claude_mate_s3/game/ship_it.h)
// draws 320x172 integer pixels onto glass at ~30 fps. This one draws the same
// 320x172 world and then makes a browser earn its keep. They share the level and
// the physics constants -- level_01.js is GENERATED from the firmware header, so
// they cannot drift -- and share nothing else.
//
// THE RENDERING IS TWO LAYERS, and that is the whole answer to "full width, but
// proportional to the device, with better text":
//
//   1. A WORLD layer, fixed at exactly 320x172. Terrain, sprites and particles
//      are drawn here at 1:1 with the device, then blitted to the visible canvas
//      with imageSmoothingEnabled = false. Nearest-neighbour upscaling at any
//      output size, so the pixel art stays pixel art -- no fractional rects, no
//      antialiasing, none of the shimmer that a ctx.scale() over 1x1 fillRects
//      produces. The playfield is a photograph of the device's screen.
//
//   2. A SCREEN layer at the canvas's real resolution (CSS size x devicePixelRatio).
//      The HUD, every string and the post-processing live here, drawn with a real
//      font at full resolution. That is where "better quality of text" comes from:
//      the device's 6x8 bitmap font upscaled 5x would be a mosaic, so the web
//      edition simply does not upscale text -- it re-renders it.
//
// Physics runs at a fixed 60 Hz on both. The constants below are the device's,
// and they are load-bearing: the level is BUILT to this jump arc.

import { LEVEL } from './level_01.js';

// ---------------------------------------------------------------------------
// The world
// ---------------------------------------------------------------------------
const W = 320, H = 172;
const T = LEVEL.tile, ROWS = LEVEL.rows, COLS = LEVEL.cols;
const HUD_H = 24, PLAY_Y = HUD_H + 12;

const GRAV = 0.30, WALK = 1.45, JUMP = -4.75, MAXFALL = 7;
const COYOTE = 6, BUFFER = 6;
const CROSS = 1159;                       // steps for a straight run, measured

const P = LEVEL.palette;
const RAMP = { '1': P.l1, '2': P.l2, '3': P.l3, '4': P.l4 };

const DIFFS = [
  { name: 'SPRINT',        days: 7, walk: 5, spd: 0.58, pr: 1,    stomp: .5, bug: 1,   prio: 2,   sub: 'the plan, as written' },
  { name: 'CRUNCH',        days: 6, walk: 6, spd: 0.72, pr: 1,    stomp: .5, bug: 1,   prio: 2,   sub: 'someone promised a demo' },
  { name: 'CODE FREEZE',   days: 5, walk: 7, spd: 0.88, pr: 1,    stomp: .5, bug: 1.5, prio: 2.5, sub: 'the branch is cut, the date is not' },
  { name: 'DEATH MARCH',   days: 4, walk: 8, spd: 1.05, pr: 0.75, stomp: .5, bug: 1.5, prio: 3,   sub: 'the slack was spent last week' },
  { name: 'HOTFIX FRIDAY', days: 3, walk: 9, spd: 1.25, pr: 0.5,  stomp: .5, bug: 2,   prio: 3,   sub: 'already broken in production' },
];
for (const d of DIFFS) d.drain = Math.round(CROSS / d.walk);

const LEVELS = 12, PKEY = 'shipit.progress.v1';

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------
const view  = document.getElementById('screen');
const vx    = view.getContext('2d', { alpha: false });
const world = document.createElement('canvas');
world.width = W; world.height = H;
const wx = world.getContext('2d', { alpha: false });
wx.imageSmoothingEnabled = false;

let S = 1, DPR = 1;                       // world->screen scale, device pixels

// "1:1" renders the canvas at exactly 320x172 CSS pixels -- the device's own
// pixel count -- so the two can be compared side by side instead of argued
// about. Off by default; the chip toggles it.
let oneToOne = false;

function resize() {
  const box = view.parentElement.getBoundingClientRect();
  DPR = Math.min(window.devicePixelRatio || 1, 3);

  let cssW, cssH;
  if (oneToOne) {
    cssW = W; cssH = H;
  } else {
    // FIT THE VIEWPORT, not just the width. Full-width alone means that on any
    // ordinary laptop the frame is taller than the space under the header, so
    // the bottom of the playfield is below the fold and what you actually see
    // is a crop -- which does not look like the device even though the canvas
    // itself is the right shape. Fit to whichever axis runs out first and the
    // whole 320:172 frame is always on screen.
    const chrome = (document.querySelector('header.bar') || {}).offsetHeight || 56;
    const maxH = Math.max(180, window.innerHeight - chrome);
    cssW = Math.max(320, Math.floor(box.width));
    cssH = Math.round(cssW * H / W);
    if (cssH > maxH) { cssH = maxH; cssW = Math.round(cssH * W / H); }
  }
  view.style.width  = cssW + 'px';
  view.style.height = cssH + 'px';
  view.width  = Math.round(cssW * DPR);
  view.height = Math.round(cssH * DPR);
  S = view.width / W;
  L = view.width / UI_W;
  vx.imageSmoothingEnabled = false;       // must be re-set: resizing resets state
  buildVignette();
}
window.addEventListener('resize', resize);

// ---------------------------------------------------------------------------
// Sound — the prototype's recipes, which the daemon also renders for the device
// ---------------------------------------------------------------------------
let AC = null, soundOn = true;
function beep(fs, d, type, vol) {
  if (!soundOn) return;
  try {
    AC = AC || new (window.AudioContext || window.webkitAudioContext)();
    if (AC.state === 'suspended') AC.resume();
    const t0 = AC.currentTime;
    fs.forEach((f, i) => {
      const o = AC.createOscillator(), g = AC.createGain();
      o.type = type || 'square';
      o.frequency.setValueAtTime(f, t0 + i * d);
      g.gain.setValueAtTime(0, t0 + i * d);
      g.gain.linearRampToValueAtTime(vol || 0.05, t0 + i * d + 0.008);
      g.gain.exponentialRampToValueAtTime(0.0008, t0 + i * d + d);
      o.connect(g); g.connect(AC.destination);
      o.start(t0 + i * d); o.stop(t0 + i * d + d + 0.02);
    });
  } catch (e) { /* audio is decoration; never let it break a frame */ }
}
const SFX = {
  jump:  () => beep([520, 760], .055),
  stomp: () => beep([300, 150], .05),
  merge: () => beep([660, 880, 1320], .065, 'square', .055),
  hit:   () => beep([180, 120], .09, 'sawtooth', .05),
  fall:  () => beep([400, 260, 160], .06, 'sawtooth'),
  ship:  () => beep([523, 659, 784, 1047, 1319], .11, 'square', .06),
  move:  () => beep([880], .03, 'square', .03),
};

// ---------------------------------------------------------------------------
// Progress
// ---------------------------------------------------------------------------
let prog = { done: 0, tier: -1, prs: 0, days: 0 };
try { const raw = localStorage.getItem(PKEY); if (raw) prog = JSON.parse(raw); } catch (e) {}
const progCount = () => { let n = 0; for (let i = 0; i < LEVELS; i++) if (prog.done & (1 << i)) n++; return n; };
function progRecord(lvl, tier, prs, days) {
  const done = prog.done | (1 << lvl);
  let best = false;
  if (prog.tier < 0 || tier > prog.tier) best = true;
  else if (tier === prog.tier) best = (prs > prog.prs) || (prs === prog.prs && days > prog.days);
  if (!best && done === prog.done) return false;
  if (best) { prog.tier = tier; prog.prs = prs; prog.days = days; }
  prog.done = done;
  try { localStorage.setItem(PKEY, JSON.stringify(prog)); } catch (e) {}
  return best;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const FRAC = { '.25': '¼', '.5': '½', '.75': '¾' };
function dstr(n) {
  const sg = n < 0 ? '-' : '', a = Math.abs(n), w = Math.floor(a);
  const f = FRAC[String(+(a - w).toFixed(2)).replace('0', '')] || '';
  return sg + ((w || !f) ? w : '') + f;
}
const solid = (c, r) => c >= 0 && c < COLS && r >= 0 && r < ROWS && LEVEL.map[r][c] !== '.';

// Takes the generated sprite object {w, h, bits} -- NOT a bare array of rows.
// It was written against a bare array first, and because JS reads .length on an
// object as undefined, `r < undefined` is false and the loop simply never ran:
// every sprite in the game drew nothing at all, silently, with no error. The
// character, the bugs and the pull requests were all invisible and the terrain
// looked fine, which made it read as a gameplay bug rather than a draw bug.
function blit(sp, x, y, col, flip) {
  wx.fillStyle = col;
  const rows = sp.bits;
  for (let r = 0; r < rows.length; r++) {
    const row = rows[r];
    let c = 0;
    while (c < row.length) {                 // run-length: fewer fillRect calls
      if (row[c] !== '1') { c++; continue; }
      const s = c;
      while (c < row.length && row[c] === '1') c++;
      const px = flip ? (x + sp.w - c) : (x + s);
      wx.fillRect(px | 0, (y + r) | 0, c - s, 1);
    }
  }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let diff = 0, tutorial = true, menuRow = 0;
let Sx;                                     // the run

function reset() {
  const d = DIFFS[diff];
  Sx = {
    x: T, y: 4 * T, vx: 0, vy: 0, onGround: false, face: 1, days: d.days, cam: 0,
    bugs:  LEVEL.bugs.map(b  => ({ c: b.col * T, r: b.row, from: b.from, to: b.to, dir: 1, dead: false })),
    prios: LEVEL.prios.map(p => ({ c: p.col * T, y: p.row * T, top: p.from * T, bot: p.to * T, dir: 1, dead: false })),
    prs:   LEVEL.prs.map(p   => ({ c: p.col, r: p.row, got: false })),
    seen: { bug: !tutorial, prio: !tutorial }, card: null,
    merged: 0, state: 'play', flash: 0, msg: '', msgCol: P.text,
    squashT: 0, t: 0, started: false, coyote: 0, buffer: 0, inv: 0,
    shipT: 0, conf: [], newBest: false,
    // wow
    shake: 0, hurt: 0, glow: new Float32Array(COLS), trail: [], parts: [], pulse: 0,
    lastDays: d.days,
  };
  jw.v = true;                              // swallow the GO that started the run
}

// ---------------------------------------------------------------------------
// Particles — the cheapest wow per line there is
// ---------------------------------------------------------------------------
function burst(x, y, n, col, spread, up) {
  for (let i = 0; i < n; i++) {
    const a = Math.random() * Math.PI * 2;
    Sx.parts.push({
      x, y,
      vx: Math.cos(a) * spread * (0.4 + Math.random()),
      vy: Math.sin(a) * spread * (0.4 + Math.random()) - (up || 0),
      life: 18 + Math.random() * 14, max: 32, col,
      g: 0.12 + Math.random() * 0.1,
    });
  }
  if (Sx.parts.length > 260) Sx.parts.splice(0, Sx.parts.length - 260);
}

// ---------------------------------------------------------------------------
// Input — keyboard, or the device
// ---------------------------------------------------------------------------
const keys = { left: false, right: false, jump: false }, jw = { v: false };
let controller = 'KEYBOARD';

const KEYMAP = {
  ArrowLeft: 'left', a: 'left', A: 'left',
  ArrowRight: 'right', d: 'right', D: 'right',
  ArrowUp: 'jump', w: 'jump', W: 'jump', ' ': 'jump',
};
addEventListener('keydown', e => {
  if (e.key === 'Escape' || e.key === 'Backspace') { fourth(); e.preventDefault(); return; }
  const k = KEYMAP[e.key];
  if (!k) return;
  e.preventDefault();
  if (Sx.state !== 'play' && !keys[k]) nav(k);
  keys[k] = true;
});
addEventListener('keyup', e => { const k = KEYMAP[e.key]; if (k) { keys[k] = false; e.preventDefault(); } });

// The device speaks in edges: +P / -P (down / up) when it is in controller mode,
// and bare P / N / G / M otherwise. Bare codes are events with a 400/200 ms
// auto-repeat, which is right for a menu and unplayable as a gamepad -- so they
// are given a short synthetic hold rather than being dropped.
const BTN = { P: 'left', N: 'right', G: 'jump' };
const pulseTimers = {};
function deviceButton(code) {
  if (code === 'M') { fourth(); return; }
  const edge = code[0] === '+' ? 1 : code[0] === '-' ? -1 : 0;
  const raw  = edge ? code.slice(1) : code;
  const k    = BTN[raw];
  if (!k) return;
  if (edge === 1)      { if (Sx.state !== 'play') nav(k); keys[k] = true; }
  else if (edge === -1) keys[k] = false;
  else {                                    // no edges available: synthesise one
    if (Sx.state !== 'play') nav(k);
    keys[k] = true;
    clearTimeout(pulseTimers[k]);
    pulseTimers[k] = setTimeout(() => { keys[k] = false; }, 130);
  }
}

function connectDevice() {
  const badge = document.getElementById('pad');
  const set = (mode, live) => {
    controller = mode;
    badge.textContent = mode === 'DEVICE' ? 'CONTROLLER · CLAUDE MATE' : 'CONTROLLER · KEYBOARD';
    badge.dataset.live = live ? '1' : '0';
  };
  set('KEYBOARD', false);
  // Same-origin when the daemon serves this page; otherwise try the loopback
  // port directly, which works when the page is opened from disk. A page served
  // over HTTPS cannot reach http://127.0.0.1 -- mixed content -- which is
  // exactly why the daemon serves the game itself.
  const base = location.protocol === 'file:' ? 'http://127.0.0.1:8788' : '';
  let es;
  try { es = new EventSource(base + '/events?grab=1'); } catch (e) { return; }
  es.onopen = () => set('DEVICE', true);
  es.onmessage = ev => {
    try {
      const m = JSON.parse(ev.data);
      if (m.btn) deviceButton(m.btn);
    } catch (e) {}
  };
  es.onerror = () => { set('KEYBOARD', false); };
}

// ---------------------------------------------------------------------------
// Menus
// ---------------------------------------------------------------------------
function nav(k) {
  if (Sx.state === 'card') { Sx.card = null; Sx.state = 'play'; jw.v = true; return; }
  if (Sx.state === 'slip' || Sx.state === 'ship') { Sx.state = 'menu'; menuRow = 0; return; }
  if (Sx.state !== 'menu') return;
  if (k === 'left')  { menuRow = (menuRow + 2) % 3; SFX.move(); }
  else if (k === 'right') { menuRow = (menuRow + 1) % 3; SFX.move(); }
  else {
    if (menuRow === 0)      { diff = (diff + 1) % DIFFS.length; Sx.days = DIFFS[diff].days; SFX.jump(); }
    else if (menuRow === 1) { tutorial = !tutorial; SFX.jump(); }
    else                    { reset(); Sx.started = true; SFX.merge(); }
  }
}
function fourth() {
  if (Sx.state === 'menu') return;
  Sx.state = 'menu'; menuRow = 0;
}

// ---------------------------------------------------------------------------
// Simulation — one 60 Hz step. Identical arithmetic to the device.
// ---------------------------------------------------------------------------
function hits(px, py) {
  const x0 = Math.floor((px + 1) / T), x1 = Math.floor((px + 12) / T);
  const y0 = Math.floor(py / T),       y1 = Math.floor((py + 11) / T);
  for (let c = x0; c <= x1; c++) for (let r = y0; r <= y1; r++) if (solid(c, r)) return true;
  return false;
}
function toast(m, c) { Sx.msg = m; Sx.msgCol = c || P.text; Sx.flash = 52; }
function gain(v, label, col) {
  const cap = DIFFS[diff].days;
  if (Sx.days >= cap) { toast(label + '  ·  already at cap', '#8b949e'); return; }
  const got = Math.min(v, cap - Sx.days);
  Sx.days += got;
  toast(label + '  +' + dstr(got) + 'd', col);
}

function step() {
  Sx.t++;
  // decay the wow
  if (Sx.shake > 0) Sx.shake *= 0.86;
  if (Sx.hurt > 0)  Sx.hurt -= 1;
  if (Sx.pulse > 0) Sx.pulse -= 1;
  for (let i = Sx.parts.length - 1; i >= 0; i--) {
    const p = Sx.parts[i];
    p.x += p.vx; p.y += p.vy; p.vy += p.g; p.vx *= 0.98;
    if (--p.life <= 0) Sx.parts.splice(i, 1);
  }
  for (let c = 0; c < COLS; c++) if (Sx.glow[c] > 0) Sx.glow[c] -= 0.018;

  if (Sx.state === 'ship') {
    Sx.shipT++;
    for (const p of Sx.conf) { p.y += p.vy; p.x += p.vx; p.rot += p.vr; if (p.y > H) p.y = -8; }
    return;
  }
  if (Sx.state !== 'play' || !Sx.started || document.hidden) return;

  const d = DIFFS[diff];
  Sx.days -= 1 / d.drain;
  if (Math.ceil(Sx.days) !== Math.ceil(Sx.lastDays)) { Sx.pulse = 10; Sx.lastDays = Sx.days; }
  if (Sx.days <= 0) {
    Sx.days = 0; Sx.state = 'slip'; toast('MILESTONE SLIPPED', P.bug); SFX.fall();
    Sx.shake = 9; return;
  }

  Sx.vx = (keys.right ? WALK : 0) - (keys.left ? WALK : 0);
  if (Sx.vx) Sx.face = Sx.vx > 0 ? 1 : -1;
  if (Sx.onGround) Sx.coyote = COYOTE; else if (Sx.coyote > 0) Sx.coyote--;
  if (keys.jump && !jw.v) Sx.buffer = BUFFER; else if (Sx.buffer > 0) Sx.buffer--;
  if (Sx.buffer > 0 && Sx.coyote > 0) {
    Sx.vy = JUMP; Sx.onGround = false; Sx.buffer = 0; Sx.coyote = 0; SFX.jump();
    burst(Sx.x + 7, Sx.y + 12, 5, P.l3, 0.7, 0.2);
  }
  if (!keys.jump && Sx.vy < -1.8) Sx.vy = -1.8;
  jw.v = keys.jump;
  Sx.vy = Math.min(Sx.vy + GRAV, MAXFALL);

  const nx = Sx.x + Sx.vx; if (!hits(nx, Sx.y)) Sx.x = nx;
  const ny = Sx.y + Sx.vy;
  const wasAir = !Sx.onGround;
  if (!hits(Sx.x, ny)) { Sx.y = ny; Sx.onGround = false; }
  else {
    if (Sx.vy > 0) { Sx.y = Math.floor((ny + 11) / T) * T - 12; Sx.onGround = true; }
    else Sx.y = Math.floor(ny / T) * T + T;
    Sx.vy = 0;
  }
  // Resting contact: without it, standing still is a three-frame sink-and-eject
  // cycle -- a 1 px 20 Hz vibration, and onGround true on a third of frames.
  if (Sx.vy >= 0 && hits(Sx.x, Sx.y + 1)) {
    Sx.y = Math.floor((Sx.y + 12) / T) * T - 12; Sx.vy = 0; Sx.onGround = true;
  }
  if (wasAir && Sx.onGround) burst(Sx.x + 7, Sx.y + 12, 4, P.l1, 0.6, -0.1);

  Sx.x = Math.max(0, Math.min(Sx.x, (COLS - 1) * T));

  // light the commit you are standing on -- the graph reacts to being walked
  if (Sx.onGround) {
    const c = Math.floor((Sx.x + 7) / T);
    if (c >= 0 && c < COLS) Sx.glow[c] = 1;
  }
  // afterimage
  if (Sx.t % 2 === 0) {
    Sx.trail.push({ x: Sx.x, y: Sx.y, f: Sx.face, a: 1 });
    if (Sx.trail.length > 7) Sx.trail.shift();
  }
  for (const t of Sx.trail) t.a *= 0.82;

  if (Sx.y > ROWS * T + 40) {
    Sx.days -= 1; toast('-1 day · fell', '#f0883e'); SFX.fall();
    Sx.shake = 7; Sx.hurt = 14;
    let c = Math.floor(Sx.x / T);
    while (c > 0 && !solid(c, 5) && !solid(c, 6)) c--;
    let r = 6; while (r > 0 && !solid(c, r)) r--;
    Sx.x = c * T; Sx.y = (r - 1) * T; Sx.vy = 0; Sx.inv = 60; Sx.trail.length = 0;
  }

  for (const b of Sx.bugs) {
    if (b.dead) continue;
    b.c += b.dir * d.spd;
    const col = Math.floor((b.c + 6) / T);
    if (col <= b.from || col >= b.to || !solid(col, b.r + 1)) b.dir *= -1;
    const bx = b.c, by = b.r * T + 6;
    if (Math.abs(bx - Sx.x) < 12 && Math.abs(by - Sx.y) < 12) {
      if (Sx.vy > .6 && Sx.y < by - 2) {
        b.dead = true; Sx.vy = -3.2; Sx.squashT = 18;
        gain(d.stomp, 'FIXED', P.l2); SFX.stomp();
        burst(bx + 6, by + 4, 16, P.bug, 1.5, 0.6); Sx.shake = 3;
      } else if (Sx.inv <= 0) {
        Sx.days -= d.bug; toast('-' + dstr(d.bug) + 'd · bug', P.bug); SFX.hit();
        Sx.x -= Sx.face * 15; Sx.vy = -2.5; Sx.inv = 75;
        Sx.shake = 8; Sx.hurt = 16; burst(Sx.x + 7, Sx.y + 6, 12, P.bug, 1.2, 0.4);
        Sx.trail.length = 0;
      }
    }
  }
  if (Sx.inv > 0) Sx.inv--;

  for (const p of Sx.prios) {
    if (p.dead) continue;
    p.y += p.dir * (d.spd * 1.3);
    if (p.y <= p.top || p.y >= p.bot) p.dir *= -1;
    if (Math.abs(p.c - Sx.x) < 13 && Math.abs(p.y - Sx.y) < 13 && Sx.inv <= 0) {
      Sx.days -= d.prio; toast('RE-SCOPED  -' + dstr(d.prio) + 'd', P.prio); SFX.hit();
      Sx.x = Math.max(0, Sx.x - 46); Sx.vy = -2.2; Sx.inv = 80;
      Sx.shake = 11; Sx.hurt = 20; burst(p.c + 6, p.y + 6, 20, P.prio, 1.6, 0.3);
      Sx.trail.length = 0;
    }
  }

  for (const p of Sx.prs) {
    if (p.got) continue;
    if (Math.abs(p.c * T - Sx.x) < 14 && Math.abs(p.r * T - Sx.y) < 14) {
      p.got = true; Sx.merged++;
      gain(d.pr, 'PR MERGED', P.pr); SFX.merge();
      burst(p.c * T + 5, p.r * T + 5, 18, P.pr, 1.3, 0.5);
      Sx.glow[p.c] = 1.4;
    }
  }

  if (Sx.flash > 0) Sx.flash--;
  if (Sx.squashT > 0) Sx.squashT--;

  const right = Sx.cam + W;
  if (!Sx.seen.bug && Sx.bugs.some(b => !b.dead && b.c > Sx.cam + 40 && b.c < right - 24)) {
    Sx.seen.bug = true; Sx.card = 'bug'; Sx.state = 'card'; SFX.hit();
  } else if (!Sx.seen.prio && Sx.prios.some(p => !p.dead && p.c > Sx.cam + 40 && p.c < right - 24)) {
    Sx.seen.prio = true; Sx.card = 'prio'; Sx.state = 'card'; SFX.hit();
  }

  if (Math.floor(Sx.x / T) >= COLS - 2) {
    Sx.state = 'ship'; Sx.shipT = 0; SFX.ship(); Sx.shake = 6;
    Sx.newBest = progRecord(0, diff, Sx.merged, Math.max(0, Sx.days));
    for (let i = 0; i < 90; i++) Sx.conf.push({
      x: Math.random() * W, y: -Math.random() * 120,
      vy: .7 + Math.random() * 2.1, vx: (Math.random() - .5) * .9,
      rot: Math.random() * 6, vr: (Math.random() - .5) * .3,
      c: [P.l1, P.l2, P.l3, P.pr, P.mate][(Math.random() * 5) | 0],
    });
  }

  Sx.cam = Math.max(0, Math.min(Sx.x - W / 2 + 7, COLS * T - W));
}

// ---------------------------------------------------------------------------
// World layer — 320x172, pixel-exact
// ---------------------------------------------------------------------------
function drawWorld() {
  const cam = Math.round(Sx.cam) + (Sx.shake > 0.4 ? Math.round((Math.random() - .5) * Sx.shake) : 0);
  const shakeY = Sx.shake > 0.4 ? Math.round((Math.random() - .5) * Sx.shake * 0.6) : 0;

  wx.fillStyle = P.bg; wx.fillRect(0, 0, W, H);

  // PARALLAX. Two dim graphs behind the real one, scrolling slower. The world
  // stops being a strip of tiles on black and becomes a place with depth -- and
  // it costs two loops of fillRect.
  // Two dim graphs behind the real one, scrolling slower. Kept DELIBERATELY
  // faint and small: the first pass drew them at the playfield's own tile size
  // in near-terrain colours, and the result read as a wall of boxes competing
  // with the level. Depth is meant to be felt, not read.
  for (const [k, sp, col] of [[0.22, 9, '#0f141a'], [0.45, 13, '#111820']]) {
    const off = (Sx.cam * k) % sp;
    for (let x = -sp; x < W + sp; x += sp) {
      for (let y = PLAY_Y - 6; y < PLAY_Y + ROWS * T + 6; y += sp) {
        wx.fillStyle = col;
        wx.fillRect(Math.round(x - off), y, 3, 3);
      }
    }
  }

  wx.save();
  wx.translate(0, shakeY);

  const c0 = Math.max(0, Math.floor(cam / T) - 1), c1 = Math.min(COLS - 1, c0 + 22);
  for (let c = c0; c <= c1; c++) {
    const sx = c * T - cam;
    for (let r = 0; r < ROWS; r++) {
      const sy = PLAY_Y + r * T, ch = LEVEL.map[r][c];
      if (ch === '.') {
        wx.strokeStyle = P.grid; wx.lineWidth = 1;
        wx.strokeRect(sx + 1.5, sy + 1.5, T - 3, T - 3);
      } else {
        let col = RAMP[ch];
        if (Sx.state === 'ship' && c * 3 < Sx.shipT) col = P.l1;
        else if (Sx.glow[c] > 0) col = mix(col, '#ffffff', Math.min(0.55, Sx.glow[c] * 0.5));
        wx.fillStyle = col;
        wx.fillRect(sx + 1, sy + 1, T - 2, T - 2);
      }
    }
  }

  // the milestone
  wx.fillStyle = Sx.state === 'ship' ? P.l1 : P.pr;
  wx.fillRect((COLS - 1) * T - cam + 2, PLAY_Y, 3, ROWS * T);

  for (const p of Sx.prs) {
    if (p.got) continue;
    const bob = Math.sin((Sx.t + p.c * 9) / 17) * 1.6;
    blit(LEVEL.sprites.pr, p.c * T - cam + 3, PLAY_Y + p.r * T + 3 + bob, P.pr);
  }
  for (const b of Sx.bugs) if (!b.dead) blit(LEVEL.sprites.bug, b.c - cam, PLAY_Y + b.r * T + 6, P.bug, b.dir < 0);
  for (const p of Sx.prios) if (!p.dead) blit(LEVEL.sprites.prio, p.c - cam, PLAY_Y + p.y + 2, P.prio);

  // particles
  for (const p of Sx.parts) {
    wx.fillStyle = p.col;
    wx.globalAlpha = Math.max(0, p.life / p.max);
    wx.fillRect(Math.round(p.x - cam), Math.round(p.y + PLAY_Y), 2, 2);
  }
  wx.globalAlpha = 1;

  // the afterimage, then the character
  for (const t of Sx.trail) {
    if (t.a < 0.1) continue;
    wx.globalAlpha = t.a * 0.35;
    blit(LEVEL.sprites.mate, Math.round(t.x - cam), Math.round(t.y) + PLAY_Y, P.mate, t.f < 0);
  }
  wx.globalAlpha = 1;

  let pose = LEVEL.sprites.mate;
  if (Sx.squashT > 0) pose = LEVEL.sprites.mateSquash;
  else if (!Sx.onGround && Sx.vy < -1) pose = LEVEL.sprites.mateStretch;
  const ph = pose.h;                        // bottom-align from the sprite itself
  if (!(Sx.inv > 0 && (Sx.t >> 2) % 2))
    blit(pose, Math.round(Sx.x - cam), Math.round(Sx.y) + PLAY_Y + (12 - ph), P.mate, Sx.face < 0);

  if (Sx.state === 'ship') for (const p of Sx.conf) {
    wx.fillStyle = p.c; wx.fillRect(p.x | 0, p.y | 0, 3, 3);
  }
  wx.restore();
}

function mix(a, b, t) {
  const pa = [1, 3, 5].map(i => parseInt(a.substr(i, 2), 16));
  const pb = [1, 3, 5].map(i => parseInt(b.substr(i, 2), 16));
  return '#' + pa.map((v, i) => Math.round(v + (pb[i] - v) * t).toString(16).padStart(2, '0')).join('');
}

// ---------------------------------------------------------------------------
// Screen layer — real resolution, real font
// ---------------------------------------------------------------------------
let vign = null;
function buildVignette() {
  vign = document.createElement('canvas');
  vign.width = view.width; vign.height = view.height;
  const g = vign.getContext('2d');
  const r = g.createRadialGradient(
    view.width / 2, view.height / 2, view.height * 0.25,
    view.width / 2, view.height / 2, view.width * 0.72);
  r.addColorStop(0, 'rgba(0,0,0,0)');
  r.addColorStop(1, 'rgba(0,0,0,0.55)');
  g.fillStyle = r; g.fillRect(0, 0, view.width, view.height);
  // scanlines, at device-pixel granularity so they never moiré
  // Scanlines at DEVICE-pixel granularity, one line per world row, and faint.
  // At 0.16 over a 4x upscale they striped every terrain tile and the playfield
  // read as corduroy.
  const gap = Math.max(2, Math.round(S));
  g.fillStyle = 'rgba(0,0,0,0.055)';
  for (let y = 0; y < view.height; y += gap) g.fillRect(0, y, view.width, Math.max(1, gap * 0.34));
}

const FONT = '"SF Mono", ui-monospace, Menlo, Consolas, monospace';

// THE UI HAS ITS OWN COORDINATE SYSTEM, and this is the part that makes the web
// edition a web thing rather than a magnified phone screen.
//
// The playfield must stay in the device's 320x172 -- the level is built to it,
// so a wider view would change what the game IS. But the HUD and the menus have
// no such constraint, and drawing them in 320-space multiplied by the display
// scale is what a naive port does: at 1280 px wide every label comes out 4x too
// big, the layout reads as a screenshot someone zoomed, and rows laid out for
// 172 px of height collide.
//
// So UI is designed in UI_W x UI_H -- the same ASPECT as the device, four times
// the room -- and scaled to fit. Same proportions, web-sized type.
const UI_W = 1000, UI_H = Math.round(1000 * H / W);   // 1000 x 538
let L = 1;                                            // ui-units -> device px

function txt(s, x, y, size, col, align, weight) {
  vx.font = `${weight || 600} ${Math.round(size * L)}px ${FONT}`;
  vx.fillStyle = col;
  vx.textAlign = align || 'left';
  vx.textBaseline = 'alphabetic';
  vx.fillText(s, Math.round(x * L), Math.round(y * L));
}
const tw = (s, size, weight) => {
  vx.font = `${weight || 600} ${Math.round(size * L)}px ${FONT}`;
  return vx.measureText(s).width / L;
};
const R = (x, y, w, h, col) => { vx.fillStyle = col; vx.fillRect(x * L, y * L, w * L, h * L); };

function drawHud() {
  const d = DIFFS[diff];
  // 24/172 of the height, to the pixel -- the device's own HUD band. 70 was
  // 22.4 device rows, which is close enough to look right on its own and
  // wrong next to the real thing.
  const hud = UI_H * HUD_H / H;                 // 75.07
  R(0, 0, UI_W, hud, 'rgba(13,17,23,0.92)');
  R(0, hud - 1.2, UI_W, 1.2, P.grid);

  txt(LEVEL.name, 26, 45, 25, P.text, 'left', 700);
  txt('\u21c4 ' + Sx.merged + '/' + LEVEL.prs.length, 430, 45, 25, P.pr, 'left', 700);

  const days = Math.max(0, Sx.days), frac = Math.min(1, days / d.days);
  const bw = 250, bx = UI_W - 26 - 108 - bw, by = 21, bh = 28;
  R(bx, by, bw, bh, '#21262d');
  R(bx, by, bw * frac, bh, frac > .5 ? P.l2 : frac > .25 ? P.prio : P.bug);
  if (frac <= .25) {                        // the deadline breathes when it bites
    vx.globalAlpha = 0.25 + 0.25 * Math.sin(Sx.t / 5);
    R(bx, by, bw * frac, bh, P.bug);
    vx.globalAlpha = 1;
  }
  const pop = Sx.pulse > 0 ? 1 + Sx.pulse / 30 : 1;
  txt(Math.ceil(days) + 'd', UI_W - 26 - tw(Math.ceil(days) + 'd', 30 * pop, 700), 48,
      30 * pop, Sx.pulse > 0 ? '#ffffff' : P.text, 'left', 700);
}

function centre(s, y, size, col, weight) {
  txt(s, UI_W / 2 - tw(s, size, weight) / 2, y, size, col, 'left', weight);
}

function drawMenu() {
  vx.fillStyle = P.bg; vx.fillRect(0, 0, view.width, view.height);

  // a slow drift of dim commits behind the menu
  const t = Date.now() / 3000;
  for (let i = 0; i < 54; i++) {
    const x = ((i * 167 + t * 90) % (UI_W + 90)) - 45;
    const y = 40 + ((i * 113) % 430);
    R(x, y, 34, 34, ['#0f151c', '#111a22', '#132029'][i % 3]);
  }

  txt('SHIP IT', 56, 136, 84, P.l1, 'left', 800);
  txt('walk your contribution graph to the milestone', 60, 176, 21, '#8b949e', 'left', 500);

  const d = DIFFS[diff];
  const rows = [
    ['DIFFICULTY', d.name, d.sub],
    ['TUTORIAL', tutorial ? 'ON' : 'OFF',
      tutorial ? 'freeze and explain each new enemy' : 'no cards, straight in'],
    ['START SPRINT', '',
      d.days + ' days \u00b7 the walk alone costs ' + d.walk + ' \u00b7 ' + LEVEL.prs.length + ' PRs on the map'],
  ];
  let y = 262;
  for (let i = 0; i < rows.length; i++) {
    const sel = i === menuRow;
    if (sel) { R(30, y - 40, UI_W - 60, 70, '#161b22'); R(30, y - 40, 7, 70, P.mate); }
    txt(rows[i][0], 54, y, 30, sel ? P.text : '#8b949e', 'left', 700);
    if (rows[i][1]) {
      const vwid = tw(rows[i][1], 30, 700);
      txt(rows[i][1], UI_W - 56 - vwid, y, 30, (i === 1 && !tutorial) ? '#8b949e' : P.l2, 'left', 700);
    }
    txt(rows[i][2], 54, y + 28, 18, '#6e7681', 'left', 500);
    y += 86;
  }

  const rec = prog.tier < 0 ? 'no milestone shipped yet'
    : 'best: ' + DIFFS[prog.tier].name + '   ' + prog.prs + '/' + LEVEL.prs.length
      + ' PRs   ' + dstr(prog.days) + 'd left';
  centre(rec, 496, 18, prog.tier < 0 ? '#484f58' : P.pr, 600);
  centre(controller === 'DEVICE'
    ? 'the device is your controller  \u00b7  outer keys move  \u00b7  \u25cb changes or starts'
    : '\u2190 \u2192 move  \u00b7  \u2191 change or start  \u00b7  esc back', 524, 17, '#6e7681', 500);
}

function drawCard() {
  const bug = Sx.card === 'bug';
  const col = bug ? P.bug : P.prio;
  vx.fillStyle = 'rgba(13,17,23,0.94)'; vx.fillRect(0, 0, view.width, view.height);
  vx.strokeStyle = col; vx.lineWidth = Math.max(1, L * 1.4);
  vx.strokeRect(56 * L, 82 * L, (UI_W - 112) * L, 374 * L);

  // the sprite, blown up with nearest-neighbour so it stays pixel art
  const sp = bug ? LEVEL.sprites.bug : LEVEL.sprites.prio;
  const tmp = document.createElement('canvas');
  tmp.width = sp.w; tmp.height = sp.h;
  const tg = tmp.getContext('2d');
  tg.fillStyle = col;
  for (let r = 0; r < sp.h; r++) for (let c = 0; c < sp.w; c++)
    if (sp.bits[r][c] === '1') tg.fillRect(c, r, 1, 1);
  vx.imageSmoothingEnabled = false;
  vx.drawImage(tmp, 106 * L, 150 * L, sp.w * 8 * L, sp.h * 8 * L);

  txt(bug ? 'BUG' : 'PRIORITY CHANGE', 300, 182, 42, col, 'left', 800);
  const d = DIFFS[diff];
  const lines = bug
    ? [['Patrols a commit. Turns at every edge.', ''],
       ['Stomp it from above', '+\u00bd day'],
       ['Let it reach you', '\u2212' + dstr(d.bug) + ' days']]
    : [['Drifts up and down. Denies a whole lane.', ''],
       ['CANNOT be stomped', 'go around it'],
       ['If it reaches you', '\u2212' + dstr(d.prio) + ' days, pushed back']];
  let y = 250;
  for (const [l, r] of lines) {
    txt(l, 300, y, 22, r ? P.text : '#8b949e', 'left', r ? 600 : 500);
    if (r) txt(r, UI_W - 90 - tw(r, 22, 700), y, 22, col, 'left', 700);
    y += 46;
  }
  centre('any key to continue', 428, 18, '#6e7681', 500);
}

function drawEnd() {
  const win = Sx.state === 'ship';
  if (win && Sx.shipT <= 26) return;
  const col = win ? P.l2 : P.bug;
  vx.fillStyle = 'rgba(13,17,23,0.94)';
  vx.fillRect(0, 125 * L, view.width, 341 * L);
  R(0, 125, UI_W, 1.4, col);
  R(0, 465, UI_W, 1.4, col);

  centre(win ? 'MILESTONE SHIPPED' : 'MILESTONE SLIPPED', 196, 46, win ? P.l1 : P.bug, 800);
  if (win) {
    centre(LEVEL.name + ' PASSED', 250, 22, P.text, 600);
    centre(Sx.merged + '/' + LEVEL.prs.length + ' merged   ' + dstr(Math.max(0, Sx.days))
           + 'd left   ' + DIFFS[diff].name, 292, 21, '#8b949e', 500);
    centre(Sx.newBest ? 'NEW BEST \u2014 PROGRESS SAVED' : 'PROGRESS SAVED', 350, 23,
           Sx.newBest ? P.pr : '#8b949e', 700);
    centre('milestone ' + progCount() + ' of ' + LEVELS + ' \u2014 the next '
           + (LEVELS - progCount()) + ' are coming soon', 406, 19, '#8b949e', 500);
  } else {
    centre(Sx.merged + '/' + LEVEL.prs.length + ' merged \u2014 ran out of days', 274, 22, '#8b949e', 500);
    centre('the deadline is the fail state, and it is the honest one', 316, 19, '#6e7681', 500);
  }
  centre('any key to go back', 444, 18, '#484f58', 500);
}

function drawScreen() {
  if (Sx.state === 'menu') { drawMenu(); return; }
  vx.drawImage(world, 0, 0, view.width, view.height);
  drawHud();
  // Not while an end panel is up: the panel already says MILESTONE SLIPPED in
  // 46px, and the toast repeating it at the bottom reads as a rendering bug.
  if (Sx.flash > 0 && Sx.msg && Sx.state !== 'ship' && Sx.state !== 'slip') {
    vx.globalAlpha = Math.min(1, Sx.flash / 14);
    centre(Sx.msg, 516, 22, Sx.msgCol, 700);
    vx.globalAlpha = 1;
  }
  if (Sx.state === 'card') drawCard();
  if (Sx.state === 'ship' || Sx.state === 'slip') drawEnd();

  if (vign) { vx.globalAlpha = 0.9; vx.drawImage(vign, 0, 0); vx.globalAlpha = 1; }
  if (Sx.hurt > 0) {
    vx.fillStyle = 'rgba(196,48,43,' + (0.26 * Sx.hurt / 20) + ')';
    vx.fillRect(0, 0, view.width, view.height);
  }
}

// ---------------------------------------------------------------------------
// Loop — fixed 60 Hz simulation, render at whatever the display does
// ---------------------------------------------------------------------------
let acc = 0, last = 0;
function frame(now) {
  if (!last) last = now;
  let dt = now - last; last = now;
  if (dt > 100) dt = 100;                   // a stalled tab must not fast-forward
  acc += dt;
  let n = 0;
  while (acc >= 1000 / 60 && n < 6) { step(); acc -= 1000 / 60; n++; }
  drawWorld();
  drawScreen();
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------------------
reset();
Sx.state = 'menu';
resize();
connectDevice();
requestAnimationFrame(frame);

// Read-only probe for the harness. It never writes state, so it cannot become a
// cheat by accident; __finish is separate and obviously named.
window.__shipit = () => ({
  state: Sx.state, x: Sx.x, y: Sx.y, cam: Sx.cam, days: Sx.days, onGround: Sx.onGround,
  merged: Sx.merged, inv: Sx.inv, parts: Sx.parts.length, trail: Sx.trail.length,
  diff: DIFFS[diff].name, controller, S, L, uiW: UI_W,
});
window.__finish = (prs, days) => {
  Sx.merged = prs; Sx.days = days; Sx.state = 'ship'; Sx.shipT = 40;
  Sx.newBest = progRecord(0, diff, prs, days);
  return { newBest: Sx.newBest, prog: { ...prog } };
};
window.__btn = deviceButton;   // exercise the device path without a device
window.__wipeProgress = () => { prog = { done: 0, tier: -1, prs: 0, days: 0 };
  try { localStorage.removeItem(PKEY); } catch (e) {} return { ...prog }; };

document.getElementById('one')?.addEventListener('click', e => {
  oneToOne = !oneToOne;
  e.currentTarget.dataset.on = oneToOne ? '1' : '0';
  resize();
});

document.getElementById('snd')?.addEventListener('click', e => {
  soundOn = !soundOn;
  e.currentTarget.dataset.on = soundOn ? '1' : '0';
  if (soundOn) SFX.merge();
});
