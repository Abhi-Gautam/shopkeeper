// Shop floor picture. Draws events. Owns no shop logic.
(function () {
  "use strict";

  var W = 960;
  var H = 540;
  var canvas = document.getElementById("shop");
  var ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;

  var staffInput = document.getElementById("staff");
  var arrivalInput = document.getElementById("arrival");
  var staffVal = document.getElementById("staff-val");
  var arrivalVal = document.getElementById("arrival-val");

  var HUD_H = 112;
  var CHART_TOP = 432;
  var DOOR_X = 22;
  var DOOR_Y = 348;
  var LINE_Y = 400;
  var SERVE_Y = 300;
  var COUNTER_Y = 236;
  var COUNTER_X = 140;
  var COUNTER_W = 700;
  var STAFF_Y = 228;
  var LINE_X0 = 96;
  var LINE_GAP = 78;
  var MAX_VISIBLE = 6;
  var WALK_SPEED = 90;

  var SHIRTS = ["#c45c4a", "#3d8f6e", "#c9a24a", "#6a7eb5", "#b56a8a", "#5aa7a0"];

  var state = {
    staff: 2,
    arrival: 4,
    customers: {},
    order: [],
    serving: {},
    bags: [],
    empties: [],
    looks: [],
    history: [],
    liveTokens: null,
    liveP95: null,
    applyingRemote: false
  };

  var postTimer = null;

  function clamp(n, a, b) {
    return Math.max(a, Math.min(b, n));
  }

  function nowSec() {
    return performance.now() / 1000;
  }

  function truncate(text, n) {
    text = String(text == null ? "" : text);
    if (text.length <= n) return text;
    return text.slice(0, n - 1) + "…";
  }

  function shirtFor(id) {
    var h = 0;
    var s = String(id);
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return SHIRTS[h % SHIRTS.length];
  }

  function asNumber(v) {
    if (typeof v === "number" && isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "" && isFinite(Number(v))) return Number(v);
    return null;
  }

  function setSliders(staff, arrival, fromRemote) {
    var staffN = asNumber(staff);
    var arrivalN = asNumber(arrival);
    if (staffN != null) state.staff = clamp(Math.round(staffN), 1, 3);
    if (arrivalN != null) state.arrival = clamp(Math.round(arrivalN), 1, 12);
    if (fromRemote && postTimer) {
      clearTimeout(postTimer);
      postTimer = null;
    }
    state.applyingRemote = !!fromRemote;
    staffInput.value = String(state.staff);
    arrivalInput.value = String(state.arrival);
    staffVal.textContent = String(state.staff);
    arrivalVal.textContent = String(state.arrival);
    state.applyingRemote = false;
  }

  function schedulePost() {
    if (state.applyingRemote) return;
    if (location.protocol !== "http:" && location.protocol !== "https:") return;
    if (postTimer) clearTimeout(postTimer);
    postTimer = setTimeout(function () {
      postTimer = null;
      var body = JSON.stringify({
        staff: state.staff,
        arrival: state.arrival
      });
      fetch("/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body
      }).catch(function () {});
    }, 150);
  }

  staffInput.addEventListener("input", function () {
    setSliders(Number(staffInput.value), state.arrival, false);
    schedulePost();
  });
  arrivalInput.addEventListener("input", function () {
    setSliders(state.staff, Number(arrivalInput.value), false);
    schedulePost();
  });

  function customer(id) {
    var c = state.customers[id];
    if (c) return c;
    c = {
      id: id,
      ask: "",
      shirt: shirtFor(id),
      phase: "walk-in",
      worker: null,
      reply: "",
      status: null,
      x: DOOR_X + 16,
      y: DOOR_Y + 28,
      targetX: LINE_X0,
      targetY: LINE_Y,
      born: nowSec()
    };
    state.customers[id] = c;
    state.order.push(id);
    return c;
  }

  function removeCustomer(id) {
    delete state.customers[id];
    state.order = state.order.filter(function (x) { return x !== id; });
    Object.keys(state.serving).forEach(function (w) {
      if (state.serving[w] === id) delete state.serving[w];
    });
  }

  function applyEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    var type = ev.type;
    try {
      if (type === "hello" || type === "config") {
        setSliders(ev.staff, ev.arrival, true);
        return;
      }
      if (type === "arrive") {
        if (ev.id == null) return;
        var c = customer(String(ev.id));
        c.ask = String(ev.ask == null ? "" : ev.ask);
        if (c.phase === "gone") {
          c.phase = "walk-in";
          c.x = DOOR_X + 16;
          c.y = DOOR_Y + 28;
          c.status = null;
          c.reply = "";
          c.worker = null;
        }
        return;
      }
      if (type === "assign") {
        if (ev.id == null) return;
        var a = customer(String(ev.id));
        var w = clamp(Number(ev.worker) || 0, 0, 2);
        Object.keys(state.serving).forEach(function (key) {
          if (state.serving[key] === a.id) delete state.serving[key];
        });
        state.serving[w] = a.id;
        a.worker = w;
        a.phase = "walk-counter";
        a.status = null;
        return;
      }
      if (type === "reply") {
        if (ev.id == null) return;
        var r = customer(String(ev.id));
        var rw = ev.worker == null ? r.worker : clamp(Number(ev.worker) || 0, 0, 2);
        if (rw == null) rw = 0;
        Object.keys(state.serving).forEach(function (key) {
          if (state.serving[key] === r.id && Number(key) !== rw) delete state.serving[key];
        });
        state.serving[rw] = r.id;
        r.worker = rw;
        r.reply = String(ev.text == null ? "" : ev.text);
        r.status = String(ev.status || "");
        r.phase = "served";
        if (typeof ev.tokens === "number" || typeof ev.seconds === "number") {
          state.history.push({
            t: nowSec(),
            tokens: Number(ev.tokens) || 0,
            seconds: Number(ev.seconds) || 0
          });
        }
        state.looks = state.looks.filter(function (l) { return l.worker !== rw; });
        if (r.status === "sold") {
          state.bags.push({
            x: staffX(rw) - 7,
            y: COUNTER_Y - 4,
            born: nowSec()
          });
        } else if (r.status === "out") {
          state.empties.push({ id: r.id, worker: rw, born: nowSec() });
        } else if (r.status === "wait") {
          state.looks.push({ worker: rw, until: nowSec() + 2.6 });
        }
        return;
      }
      if (type === "leave") {
        if (ev.id == null) return;
        var id = String(ev.id);
        var gone = state.customers[id];
        if (!gone) return;
        gone.phase = "leave";
        Object.keys(state.serving).forEach(function (key) {
          if (state.serving[key] === id) delete state.serving[key];
        });
        return;
      }
      if (type === "metrics") {
        if (typeof ev.tokens_per_min === "number") state.liveTokens = ev.tokens_per_min;
        if (typeof ev.p95 === "number") state.liveP95 = ev.p95;
        return;
      }
    } catch (err) {
      // A bad event must not stop the picture.
    }
  }

  function ingest(raw) {
    try {
      var ev = typeof raw === "string" ? JSON.parse(raw) : raw;
      applyEvent(ev);
    } catch (err) {
      // Ignore malformed payloads.
    }
  }

  // --- tape: file open, or /events fails immediately ---

  var TAPE = [
    { at: 0.2, ev: { type: "hello", staff: 2, arrival: 4 } },
    { at: 0.6, ev: { type: "arrive", id: "c1", ask: "One bag of rice, please" } },
    { at: 1.4, ev: { type: "arrive", id: "c2", ask: "A jar of honey" } },
    { at: 2.2, ev: { type: "arrive", id: "c3", ask: "Box of tea, the plain one" } },
    { at: 2.6, ev: { type: "assign", id: "c1", worker: 0 } },
    { at: 3.8, ev: { type: "reply", id: "c1", worker: 0, text: "Rice, 1 kg, 12 left.", status: "sold", tokens: 400, seconds: 3.2 } },
    { at: 5.4, ev: { type: "leave", id: "c1" } },
    { at: 5.6, ev: { type: "assign", id: "c2", worker: 1 } },
    { at: 6.6, ev: { type: "arrive", id: "c4", ask: "Two candles and matches" } },
    { at: 7.2, ev: { type: "reply", id: "c2", worker: 1, text: "Honey is out. I can note it.", status: "out", tokens: 180, seconds: 2.1 } },
    { at: 8.6, ev: { type: "leave", id: "c2" } },
    { at: 8.8, ev: { type: "assign", id: "c3", worker: 0 } },
    { at: 9.6, ev: { type: "arrive", id: "c5", ask: "A small lamp oil bottle" } },
    { at: 10.2, ev: { type: "reply", id: "c3", worker: 0, text: "Checking the back shelf.", status: "wait", tokens: 90, seconds: 1.4 } },
    { at: 12.0, ev: { type: "reply", id: "c3", worker: 0, text: "Plain tea, box of 20.", status: "sold", tokens: 320, seconds: 4.6 } },
    { at: 13.4, ev: { type: "leave", id: "c3" } },
    { at: 13.6, ev: { type: "assign", id: "c4", worker: 1 } },
    { at: 14.2, ev: { type: "arrive", id: "c6", ask: "Soap, unscented" } },
    { at: 15.0, ev: { type: "reply", id: "c4", worker: 1, text: "Candles are on the left shelf.", status: "guide", tokens: 140, seconds: 2.8 } },
    { at: 16.2, ev: { type: "leave", id: "c4" } },
    { at: 16.4, ev: { type: "assign", id: "c5", worker: 0 } },
    { at: 17.4, ev: { type: "reply", id: "c5", worker: 0, text: "Lamp oil, small bottle.", status: "sold", tokens: 260, seconds: 2.4 } },
    { at: 18.8, ev: { type: "leave", id: "c5" } },
    { at: 19.0, ev: { type: "assign", id: "c6", worker: 0 } },
    { at: 20.0, ev: { type: "reply", id: "c6", worker: 0, text: "Soap is out today.", status: "out", tokens: 110, seconds: 1.8 } },
    { at: 21.2, ev: { type: "leave", id: "c6" } },
    { at: 22.0, ev: { type: "arrive", id: "c7", ask: "A box of matches" } },
    { at: 22.8, ev: { type: "arrive", id: "c8", ask: "Flour, one bag" } },
    { at: 23.6, ev: { type: "arrive", id: "c9", ask: "Glass jar, empty" } },
    { at: 24.2, ev: { type: "assign", id: "c7", worker: 1 } },
    { at: 25.2, ev: { type: "reply", id: "c7", worker: 1, text: "One moment.", status: "wait", tokens: 40, seconds: 0.8 } },
    { at: 26.6, ev: { type: "reply", id: "c7", worker: 1, text: "Matches, one box.", status: "sold", tokens: 150, seconds: 2.2 } },
    { at: 27.8, ev: { type: "leave", id: "c7" } }
  ];

  var tapeIndex = 0;
  var tapeStart = 0;
  var usingTape = false;

  function startTape() {
    if (usingTape) return;
    usingTape = true;
    tapeIndex = 0;
    tapeStart = nowSec();
  }

  function tickTape() {
    if (!usingTape) return;
    var elapsed = nowSec() - tapeStart;
    while (tapeIndex < TAPE.length && TAPE[tapeIndex].at <= elapsed) {
      applyEvent(TAPE[tapeIndex].ev);
      tapeIndex++;
    }
    if (tapeIndex >= TAPE.length && elapsed > TAPE[TAPE.length - 1].at + 8) {
      // Loop so a recording still has motion if left running.
      Object.keys(state.customers).forEach(removeCustomer);
      state.bags = [];
      state.empties = [];
      state.looks = [];
      tapeIndex = 0;
      tapeStart = nowSec();
    }
  }

  function connect() {
    var protocol = location.protocol;
    if (protocol !== "http:" && protocol !== "https:") {
      startTape();
      return;
    }
    var source;
    var opened = false;
    try {
      source = new EventSource("/events");
    } catch (err) {
      startTape();
      return;
    }
    source.onopen = function () {
      opened = true;
    };
    source.onmessage = function (msg) {
      opened = true;
      ingest(msg.data);
    };
    source.onerror = function () {
      // A drop after a good open keeps the last picture and lets
      // EventSource retry. A failure before any open is "no server".
      if (opened) return;
      try { source.close(); } catch (e) {}
      startTape();
    };
  }

  // --- layout helpers ---

  function staffCount() {
    return clamp(state.staff, 1, 3);
  }

  function staffX(i) {
    var n = staffCount();
    var span = COUNTER_W - 80;
    if (n === 1) return COUNTER_X + COUNTER_W / 2;
    return COUNTER_X + 40 + (span * i) / (n - 1);
  }

  function workerOf(id) {
    var found = null;
    Object.keys(state.serving).forEach(function (w) {
      if (state.serving[w] === id) found = Number(w);
    });
    return found;
  }

  function waitingIds() {
    var n = staffCount();
    return state.order.filter(function (id) {
      var c = state.customers[id];
      if (!c || c.phase === "leave" || c.phase === "gone") return false;
      var w = workerOf(id);
      if (w != null && w < n) return false;
      return true;
    });
  }

  function lineTarget(index) {
    return LINE_X0 + index * LINE_GAP;
  }

  function updateMotion(dt) {
    var waiting = waitingIds();
    waiting.forEach(function (id, i) {
      var c = state.customers[id];
      c.targetX = lineTarget(Math.min(i, MAX_VISIBLE - 1));
      c.targetY = LINE_Y;
      if (c.phase === "walk-in" || c.phase === "wait") c.phase = "wait";
    });

    Object.keys(state.serving).forEach(function (w) {
      if (Number(w) >= staffCount()) return;
      var id = state.serving[w];
      var c = state.customers[id];
      if (!c || c.phase === "leave" || c.phase === "gone") return;
      c.targetX = staffX(Number(w));
      c.targetY = SERVE_Y;
      if (c.phase !== "served") c.phase = "walk-counter";
    });

    Object.keys(state.customers).forEach(function (id) {
      var c = state.customers[id];
      if (c.phase === "leave") {
        c.x -= WALK_SPEED * dt * 1.15;
        c.y += WALK_SPEED * dt * 0.35;
        if (c.x < -40) removeCustomer(id);
        return;
      }
      var dx = c.targetX - c.x;
      var dy = c.targetY - c.y;
      var step = WALK_SPEED * dt;
      if (Math.abs(dx) <= step) c.x = c.targetX;
      else c.x += Math.sign(dx) * step;
      if (Math.abs(dy) <= step) c.y = c.targetY;
      else c.y += Math.sign(dy) * step;
    });

    var t = nowSec();
    state.bags = state.bags.filter(function (b) {
      b.y += 36 * dt;
      return t - b.born < 1.5;
    });
    state.empties = state.empties.filter(function (e) {
      return t - e.born < 1.1;
    });
    state.looks = state.looks.filter(function (l) {
      return l.until > t;
    });
    var cutoff = t - 60;
    if (state.history.length > 400) {
      state.history = state.history.filter(function (h) { return h.t >= cutoff; });
    }
  }

  // --- pixel helpers ---

  function rect(x, y, w, h, color) {
    ctx.fillStyle = color;
    ctx.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h));
  }

  function px(x, y, color) {
    ctx.fillStyle = color;
    ctx.fillRect(x, y, 1, 1);
  }

  function person(x, y, shirt, facing, pose) {
    // Feet at (x, y). Front view, so the line faces the counter.
    // pose: stand, lean, look-up, serve. facing only flips a step.
    x = Math.round(x);
    y = Math.round(y);
    var skin = "#e6c2a0";
    var hair = "#2c241c";
    var shoe = "#241c16";
    var pants = "#343c4e";
    var step = facing === "left" ? -2 : 0;

    function block(dx, dy, w, h, color) {
      rect(x + dx * 2, y + dy * 2, w * 2, h * 2, color);
    }

    if (pose === "look-up") {
      block(-2, -17, 5, 2, hair);
      block(-2, -15, 5, 3, skin);
      block(-1, -15, 1, 1, "#2a241c");
      block(1, -15, 1, 1, "#2a241c");
    } else {
      block(-2, -16, 5, 3, hair);
      block(-2, -14, 5, 3, skin);
      block(-1, -13, 1, 1, "#2a241c");
      block(1, -13, 1, 1, "#2a241c");
    }
    block(-2, -11, 5, 4, shirt);
    if (pose === "serve") {
      block(-4, -9, 2, 3, shirt);
      block(3, -9, 2, 3, shirt);
      block(-4, -6, 1, 1, skin);
      block(4, -6, 1, 1, skin);
    } else if (pose === "lean") {
      block(-4, -8, 2, 2, shirt);
      block(3, -8, 2, 2, shirt);
    } else {
      block(-4, -11, 2, 3, shirt);
      block(3, -11, 2, 3, shirt);
      block(-4, -8, 1, 1, skin);
      block(4, -8, 1, 1, skin);
    }
    block(-2, -7, 2, 4, pants);
    block(1, -7, 2, 4, pants);
    block(-2 + step, -3, 2, 1, shoe);
    block(1, -3, 2, 1, shoe);
  }

  function staffSprite(x, y, pose) {
    person(x, y, "#2c3648", "right", pose);
    var ax = Math.round(x);
    var ay = Math.round(y);
    rect(ax - 4, ay - 22, 10, 12, "#141a24");
    rect(ax - 1, ay - 20, 2, 8, "#c4a574");
  }

  function boxSprite(x, y, color) {
    rect(x, y, 14, 12, color);
    rect(x + 1, y + 1, 12, 2, "rgba(255,255,255,0.25)");
    rect(x, y + 11, 14, 2, "rgba(0,0,0,0.25)");
  }

  function jarSprite(x, y) {
    rect(x + 3, y, 6, 3, "#9eb0c4");
    rect(x + 1, y + 3, 10, 10, "#d7efe8");
    rect(x + 2, y + 6, 8, 5, "#8fbfa8");
  }

  function bagSprite(x, y) {
    rect(x, y, 14, 12, "#c4a574");
    rect(x + 1, y + 1, 12, 3, "#e0c59a");
    rect(x + 4, y - 4, 2, 5, "#8a6a3a");
    rect(x + 8, y - 4, 2, 5, "#8a6a3a");
  }

  function lamp(x, y) {
    rect(x + 3, y, 2, 16, "#5a4630");
    rect(x, y + 16, 8, 3, "#f0c36a");
    rect(x - 2, y + 18, 12, 6, "rgba(240,195,106,0.18)");
  }

  function wrapText(text, maxWidth) {
    var words = String(text).split(" ");
    var lines = [];
    var line = "";
    for (var i = 0; i < words.length; i++) {
      var trial = line ? line + " " + words[i] : words[i];
      if (ctx.measureText(trial).width > maxWidth && line) {
        lines.push(line);
        line = words[i];
      } else {
        line = trial;
      }
    }
    if (line) lines.push(line);
    return lines.slice(0, 3);
  }

  function bubble(x, y, text, color) {
    ctx.font = "12px ui-monospace, Menlo, Consolas, monospace";
    var lines = wrapText(text, 180);
    var widest = 0;
    for (var i = 0; i < lines.length; i++) {
      widest = Math.max(widest, ctx.measureText(lines[i]).width);
    }
    var bw = Math.ceil(widest) + 14;
    var bh = lines.length * 14 + 8;
    var bx = Math.round(clamp(x - bw / 2, 8, W - bw - 8));
    var by = Math.round(y - bh - 10);
    rect(bx, by, bw, bh, color);
    rect(bx, by, bw, 1, "#1a2436");
    rect(bx, by + bh - 1, bw, 1, "#1a2436");
    rect(bx, by, 1, bh, "#1a2436");
    rect(bx + bw - 1, by, 1, bh, "#1a2436");
    // tail
    var tx = Math.round(clamp(x, bx + 6, bx + bw - 8));
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(tx - 4, by + bh);
    ctx.lineTo(tx + 4, by + bh);
    ctx.lineTo(tx, by + bh + 6);
    ctx.fill();
    ctx.fillStyle = "#1a2436";
    ctx.font = "12px ui-monospace, Menlo, Consolas, monospace";
    for (var j = 0; j < lines.length; j++) {
      ctx.fillText(lines[j], bx + 7, by + 14 + j * 14);
    }
  }

  function tag(x, y, text) {
    ctx.font = "11px ui-monospace, Menlo, Consolas, monospace";
    var label = truncate(text, 18);
    var tw = Math.ceil(ctx.measureText(label).width) + 8;
    var tx = Math.round(x - tw / 2);
    rect(tx, y, tw, 14, "#3a4458");
    ctx.fillStyle = "#d5dbe6";
    ctx.fillText(label, tx + 4, y + 11);
  }

  function drawShop() {
    rect(0, HUD_H, W, CHART_TOP - HUD_H, "#1c2636");
    for (var fy = HUD_H + 70; fy < CHART_TOP; fy += 14) {
      rect(0, fy, W, 1, "rgba(0,0,0,0.14)");
    }
    rect(0, HUD_H, W, 58, "#2a211c");
    rect(0, HUD_H + 58, W, 5, "#5c4030");

    var shelfY = [HUD_H + 2, HUD_H + 28];
    for (var s = 0; s < shelfY.length; s++) {
      rect(150, shelfY[s] + 16, 680, 3, "#6a4a30");
      for (var b = 0; b < 20; b++) {
        var sx = 160 + b * 33;
        if ((b + s) % 3 === 0) jarSprite(sx, shelfY[s] + 3);
        else boxSprite(sx, shelfY[s] + 4, (b + s) % 2 ? "#8d6240" : "#c4a072");
      }
    }
    lamp(128, HUD_H + 8);
    lamp(818, HUD_H + 8);

    rect(DOOR_X, DOOR_Y, 34, 58, "#3d2a1c");
    rect(DOOR_X + 4, DOOR_Y + 4, 26, 36, "#6aa0c8");
    rect(DOOR_X + 22, DOOR_Y + 30, 3, 3, "#e0c070");
    rect(DOOR_X - 3, DOOR_Y - 6, 40, 6, "#5a4030");
    ctx.font = "11px ui-monospace, Menlo, Consolas, monospace";
    ctx.fillStyle = "#e6d3b0";
    ctx.textAlign = "left";
    ctx.fillText("IN", DOOR_X + 8, DOOR_Y - 10);

    rect(COUNTER_X, COUNTER_Y, COUNTER_W, 14, "#a56b3c");
    rect(COUNTER_X, COUNTER_Y, COUNTER_W, 3, "#e0b07a");
    rect(COUNTER_X, COUNTER_Y + 14, COUNTER_W, 18, "#6b4324");
    for (var leg = 0; leg < 6; leg++) {
      rect(COUNTER_X + 18 + leg * 120, COUNTER_Y + 32, 8, 16, "#4a301c");
    }

    var n = staffCount();
    var looking = {};
    state.looks.forEach(function (l) { looking[l.worker] = true; });
    for (var i = 0; i < n; i++) {
      var sid = state.serving[i];
      var serving = sid && state.customers[sid] && state.customers[sid].phase !== "leave";
      var pose = "lean";
      if (looking[i]) pose = "look-up";
      else if (serving) pose = "serve";
      staffSprite(staffX(i), STAFF_Y, pose);
    }

    state.bags.forEach(function (b) { bagSprite(b.x, COUNTER_Y + 1); });
    state.empties.forEach(function (e) {
      var hx = staffX(e.worker) - 6;
      rect(hx, COUNTER_Y + 2, 12, 8, "#e6c2a0");
      rect(hx + 2, COUNTER_Y + 5, 8, 2, "#c9a888");
    });

    var waiting = waitingIds();
    var extra = Math.max(0, waiting.length - MAX_VISIBLE);
    waiting.slice(0, MAX_VISIBLE).forEach(function (id, i) {
      var c = state.customers[id];
      var walking = Math.abs(c.x - c.targetX) > 2 || Math.abs(c.y - c.targetY) > 2;
      person(c.x, c.y, c.shirt, walking && (Math.floor(nowSec() * 6) % 2) ? "left" : "right", "stand");
      if (c.ask) tag(c.x, c.y - 50 - (i % 2) * 16, c.ask);
    });
    if (extra > 0) {
      ctx.font = "bold 14px ui-monospace, Menlo, Consolas, monospace";
      ctx.fillStyle = "#e8f0ff";
      ctx.textAlign = "left";
      ctx.fillText("+" + extra, DOOR_X + 40, DOOR_Y + 24);
    }

    Object.keys(state.serving).forEach(function (w) {
      if (Number(w) >= n) return;
      var id = state.serving[w];
      var c = state.customers[id];
      if (!c || c.phase === "leave" || c.phase === "gone") return;
      person(c.x, c.y, c.shirt, "right", "stand");
    });

    state.order.forEach(function (id) {
      var c = state.customers[id];
      if (!c || c.phase !== "leave") return;
      person(c.x, c.y, c.shirt, "left", "stand");
    });

    // Bubbles last, so tails still point at the speaker above the line.
    Object.keys(state.serving).forEach(function (w) {
      if (Number(w) >= n) return;
      var id = state.serving[w];
      var c = state.customers[id];
      if (!c || c.phase !== "served") return;
      var ask = truncate(c.ask || "…", 80);
      var reply = c.status === "wait"
        ? truncate(c.reply || "One moment.", 80)
        : truncate(c.reply || "", 80);
      if (reply) bubble(staffX(Number(w)), STAFF_Y - 30, reply, "#f7f4ee");
      bubble(c.x, c.y - 28, ask, "#f7f4ee");
    });
  }

  function samples() {
    var t = nowSec();
    var tokens = new Array(60);
    var lat = [];
    for (var i = 0; i < 60; i++) {
      tokens[i] = 0;
      lat[i] = [];
    }
    for (var h = 0; h < state.history.length; h++) {
      var age = t - state.history[h].t;
      if (age < 0 || age >= 60) continue;
      var bucket = 59 - Math.floor(age);
      tokens[bucket] += state.history[h].tokens;
      lat[bucket].push(state.history[h].seconds);
    }
    var p95 = new Array(60);
    for (var b = 0; b < 60; b++) {
      if (!lat[b].length) {
        p95[b] = null;
        continue;
      }
      lat[b].sort(function (a, c) { return a - c; });
      var idx = Math.min(lat[b].length - 1, Math.floor(0.95 * (lat[b].length - 1)));
      p95[b] = lat[b][idx];
    }
    var recent = state.history.filter(function (item) { return t - item.t <= 60; });
    var sum = 0;
    var all = [];
    for (var r = 0; r < recent.length; r++) {
      sum += recent[r].tokens;
      all.push(recent[r].seconds);
    }
    all.sort(function (a, c) { return a - c; });
    var computedP95 = 0;
    if (all.length) {
      computedP95 = all[Math.min(all.length - 1, Math.floor(0.95 * (all.length - 1)))];
    }
    return {
      tokens: tokens,
      p95: p95,
      tokenReadout: state.liveTokens != null ? state.liveTokens : sum,
      p95Readout: state.liveP95 != null ? state.liveP95 : computedP95
    };
  }

  function drawCharts() {
    var top = CHART_TOP;
    rect(0, top, W, 100, "#070b14");
    var gap = 16;
    var panelW = (W - 36 - gap) / 2;
    var left = 18;
    var right = left + panelW + gap;
    panel(left, top + 8, panelW, 84);
    panel(right, top + 8, panelW, 84);

    var data = samples();
    ctx.font = "12px ui-monospace, Menlo, Consolas, monospace";
    ctx.fillStyle = "#9eb0c8";
    ctx.fillText("Tokens / min", left + 10, top + 24);
    ctx.fillText("p95 response time", right + 10, top + 24);

    var maxTok = 1;
    for (var i = 0; i < 60; i++) maxTok = Math.max(maxTok, data.tokens[i]);
    var barX = left + 10;
    var barW = (panelW - 92) / 60;
    for (var t = 0; t < 60; t++) {
      var bh = (data.tokens[t] / maxTok) * 40;
      rect(barX + t * barW, top + 72 - bh, Math.max(1, barW - 1), bh, "#3d7edb");
    }

    var maxP = 1;
    for (var p = 0; p < 60; p++) {
      if (data.p95[p] != null) maxP = Math.max(maxP, data.p95[p]);
    }
    ctx.strokeStyle = "#3dba7a";
    ctx.lineWidth = 2;
    ctx.beginPath();
    var started = false;
    var plotX = right + 10;
    var plotW = panelW - 92;
    for (var k = 0; k < 60; k++) {
      if (data.p95[k] == null) continue;
      var px0 = plotX + (k + 0.5) * (plotW / 60);
      var py0 = top + 72 - (data.p95[k] / maxP) * 40;
      if (!started) {
        ctx.moveTo(px0, py0);
        started = true;
      } else {
        ctx.lineTo(px0, py0);
      }
    }
    ctx.stroke();
    ctx.fillStyle = "#3dba7a";
    for (var d = 0; d < 60; d++) {
      if (data.p95[d] == null) continue;
      rect(
        plotX + (d + 0.5) * (plotW / 60) - 2,
        top + 72 - (data.p95[d] / maxP) * 40 - 2,
        4,
        4,
        "#3dba7a"
      );
    }

    ctx.fillStyle = "#e8f0ff";
    ctx.font = "22px ui-monospace, Menlo, Consolas, monospace";
    ctx.textAlign = "right";
    ctx.fillText(formatNum(data.tokenReadout), left + panelW - 10, top + 58);
    ctx.fillText(data.p95Readout.toFixed(1) + "s", right + panelW - 10, top + 58);
    ctx.textAlign = "left";
  }

  function panel(x, y, w, h) {
    rect(x, y, w, h, "#0c1424");
    rect(x, y, w, 2, "#1e3358");
    rect(x, y + h - 2, w, 2, "#1e3358");
    rect(x, y, 2, h, "#1e3358");
    rect(x + w - 2, y, 2, h, "#1e3358");
  }

  function formatNum(n) {
    n = Math.round(n);
    return String(n);
  }

  var last = nowSec();
  function frame() {
    var t = nowSec();
    var dt = Math.min(0.05, t - last);
    last = t;
    tickTape();
    updateMotion(dt);
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, W, H);
    rect(0, 0, W, H, "#070b14");
    rect(12, 8, W - 24, HUD_H - 16, "#0c1424");
    rect(12, 8, (W - 36) / 2, HUD_H - 16, "#0c1424");
    rect(12, 8, W - 24, 2, "#1e3358");
    rect(12, HUD_H - 10, W - 24, 2, "#1e3358");
    drawShop();
    drawCharts();
    requestAnimationFrame(frame);
  }

  setSliders(2, 4, true);
  connect();
  requestAnimationFrame(frame);
})();
