// Shop floor picture. Draws events. Owns no shop logic.
//
// Three surfaces, one state:
//   canvas  448x252 native, integer-scaled  - the room and the people
//   overlay DOM on top of the canvas        - every word, so text stays sharp
//   rail    DOM list                        - one card per turn, step by step
//
// Every number shown arrived in an event. Nothing is guessed here: token
// counts come from the usage block, step times from around the calls, and
// guide/buy fields from what DuckDB answered.
(function () {
  "use strict";

  // ---------- the room, in native pixels ----------

  var W = 448;
  var H = 252;

  var WALL_H       = 172;   // wall meets floor behind the counter
  var SHELF_BOARDS = [62, 104];
  var SIGN_Y       = 118;

  var COUNTER_X    = 104;
  var COUNTER_W    = 338;   // 104..442
  var COUNTER_TOP  = 170;
  var SURF_H       = 6;
  var FRONT_H      = 22;    // 176..198
  var LEG_H        = 10;

  var PERSON_H     = 56;
  var STAFF_FEET   = 196;   // behind the counter
  var SERVE_FEET   = 206;   // at the counter, our side
  var QUEUE_FEET   = 240;   // in the line

  var DOOR_X = 0, DOOR_Y = 174, DOOR_W = 34, DOOR_H = 66;

  var QUEUE_X0 = 56, QUEUE_GAP = 34, MAX_VISIBLE = 6;
  var WALK = 118;           // native px per second

  var SHIRTS = [
    "#c0574a", "#3c8b6b", "#c2983f", "#5f74ad", "#ab6385",
    "#4f9e97", "#b2713e", "#7b6bb0"
  ];

  var canvas = document.getElementById("shop");
  var ctx = canvas.getContext("2d");
  var frame = document.getElementById("frame");
  var stage = document.getElementById("stage");
  var overlay = document.getElementById("overlay");
  var turnsEl = document.getElementById("turns");
  var emptyEl = document.getElementById("empty");

  var staffInput = document.getElementById("staff");
  var arrivalInput = document.getElementById("arrival");
  var staffVal = document.getElementById("staff-val");
  var arrivalVal = document.getElementById("arrival-val");
  var liveEl = document.getElementById("live");
  var liveText = document.getElementById("live-text");
  var modelEl = document.getElementById("model");

  // The backing store is sized here, not in the markup, so the drawing
  // surface and the native grid can never drift apart.
  canvas.width = W;
  canvas.height = H;
  ctx.imageSmoothingEnabled = false;

  // ---------- state ----------

  var state = {
    staff: 2,
    arrival: 4,
    workers: 3,
    cap: 8,
    model: "",
    people: {},        // cid -> person
    order: [],         // cid arrival order
    atCounter: {},     // worker index -> cid
    active: {},        // worker index -> {kind, name, since}
    bags: [],
    flashes: [],       // shelf sweeps from a guide call
    gaps: [],          // empty-shelf blinks from an out_of_stock
    depth: 0,
    dropped: 0,
    busy: [],
    tally: { turns: 0, sold: 0, out: 0, guide: 0, error: 0, guides: 0, buys: 0, tokens: 0 },
    latNow: 0,
    tokNow: 0,
    source: "",
    latHist: [],
    tokHist: [],
    turns: {},         // tid -> {el, steps, ...}
    tidOrder: [],
    applyingRemote: false
  };

  var postTimer = null;

  // ---------- small helpers ----------

  function clamp(n, a, b) { return Math.max(a, Math.min(b, n)); }
  function nowSec() { return performance.now() / 1000; }

  function num(v) {
    if (typeof v === "number" && isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "" && isFinite(Number(v))) return Number(v);
    return null;
  }

  function cut(text, n) {
    text = String(text == null ? "" : text);
    return text.length <= n ? text : text.slice(0, n - 1) + "…";
  }

  function hash(s) {
    var h = 0;
    s = String(s);
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }

  function shirtFor(id) { return SHIRTS[hash(id) % SHIRTS.length]; }

  function secs(v) {
    v = Number(v) || 0;
    if (v >= 10) return v.toFixed(0) + "s";
    if (v >= 1) return v.toFixed(1) + "s";
    if (v >= 0.01) return (v * 1000).toFixed(0) + "ms";
    if (v > 0) return (v * 1000).toFixed(1) + "ms";
    return "0ms";
  }

  function compact(n) {
    n = Math.round(Number(n) || 0);
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 10000) return Math.round(n / 1000) + "k";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  function shortModel(id) {
    var s = String(id || "");
    if (s.indexOf("/") >= 0) s = s.split("/").pop();
    return s.replace(/:free$/, "");
  }

  // ---------- controls ----------

  function paintSlider(input) {
    var min = Number(input.min), max = Number(input.max);
    var pct = ((Number(input.value) - min) / (max - min)) * 100;
    input.style.setProperty("--fill", pct + "%");
  }

  function setSliders(staff, arrival, fromRemote) {
    var s = num(staff), a = num(arrival);
    if (s != null) state.staff = clamp(Math.round(s), 1, state.workers);
    if (a != null) state.arrival = clamp(Math.round(a), 1, 12);
    if (fromRemote && postTimer) { clearTimeout(postTimer); postTimer = null; }
    state.applyingRemote = !!fromRemote;
    staffInput.value = String(state.staff);
    arrivalInput.value = String(state.arrival);
    staffVal.innerHTML = state.staff + "<small>/" + state.workers + "</small>";
    arrivalVal.innerHTML = state.arrival + "<small>/min</small>";
    paintSlider(staffInput);
    paintSlider(arrivalInput);
    state.applyingRemote = false;
  }

  function schedulePost() {
    if (state.applyingRemote) return;
    if (location.protocol !== "http:" && location.protocol !== "https:") return;
    if (postTimer) clearTimeout(postTimer);
    postTimer = setTimeout(function () {
      postTimer = null;
      fetch("/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ staff: state.staff, arrival: state.arrival })
      }).catch(function () {});
    }, 140);
  }

  staffInput.addEventListener("input", function () {
    setSliders(Number(staffInput.value), null, false);
    schedulePost();
  });
  arrivalInput.addEventListener("input", function () {
    setSliders(null, Number(arrivalInput.value), false);
    schedulePost();
  });

  // ---------- people ----------

  function person(cid) {
    var p = state.people[cid];
    if (p) return p;
    p = {
      cid: cid,
      ask: "",
      turn: 1,
      turns: 1,
      shirt: shirtFor(cid),
      phase: "walk-in",
      worker: null,
      reply: "",
      status: null,
      bought: false,
      x: DOOR_X + 12,
      y: QUEUE_FEET,
      tx: QUEUE_X0,
      ty: QUEUE_FEET,
      step: Math.random() * 10,
      lastTx: null,
      lastTy: null,
      walkSince: nowSec(),
      born: nowSec()
    };
    state.people[cid] = p;
    state.order.push(cid);
    return p;
  }

  function dropPerson(cid) {
    delete state.people[cid];
    state.order = state.order.filter(function (x) { return x !== cid; });
    Object.keys(state.atCounter).forEach(function (w) {
      if (state.atCounter[w] === cid) delete state.atCounter[w];
    });
  }

  function staffN() { return clamp(state.staff, 1, state.workers); }

  function workerX(i) {
    var n = staffN();
    var inner = COUNTER_W - 96;
    if (n <= 1) return Math.round(COUNTER_X + COUNTER_W / 2);
    return Math.round(COUNTER_X + 48 + (inner * i) / (n - 1));
  }

  function queueX(i) { return QUEUE_X0 + i * QUEUE_GAP; }

  // Either side of the counter, so neither one hides the other.
  function keeperX(i) { return workerX(i) - 13; }
  function serveX(i) { return workerX(i) + 15; }

  function waitingIds() {
    return state.order.filter(function (cid) {
      var p = state.people[cid];
      return p && p.phase === "wait";
    });
  }

  // ---------- events ----------

  function applyEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    try { route(ev); } catch (err) { /* a bad event must not stop the picture */ }
  }

  function route(ev) {
    switch (ev.type) {

      case "hello":
        if (num(ev.workers)) state.workers = clamp(num(ev.workers), 1, 6);
        if (num(ev.cap)) state.cap = num(ev.cap);
        staffInput.max = String(state.workers);
        state.model = ev.model || "";
        modelEl.textContent = state.model
          ? state.model + (num(ev.conversations) ? "  ·  " + ev.conversations + " conversations" : "")
          : "";
        if (ev.tally) Object.assign(state.tally, ev.tally);
        setSliders(ev.staff, ev.arrival, true);
        paintStrip();
        return;

      case "config":
        setSliders(ev.staff, ev.arrival, true);
        return;

      case "arrive": {
        var p = person(String(ev.cid));
        p.ask = String(ev.text == null ? "" : ev.text);
        p.turn = num(ev.turn) || 1;
        p.turns = num(ev.turns) || 1;
        p.reply = "";
        p.status = null;
        if (ev.queued === false) {
          // A follow-up. Already at the counter, keep the position.
          p.phase = "asking";
        } else if (p.phase === "walk-in") {
          var slot = waitingIds().length;
          p.x = DOOR_X + 12;
          p.y = QUEUE_FEET;
          p.tx = queueX(Math.min(slot, MAX_VISIBLE - 1));
          p.ty = QUEUE_FEET;
          p.phase = "wait";
        }
        if (ev.tid) startTurn(ev, p);
        return;
      }

      case "assign": {
        var a = person(String(ev.cid));
        var w = clamp(num(ev.worker) || 0, 0, state.workers - 1);
        Object.keys(state.atCounter).forEach(function (k) {
          if (state.atCounter[k] === a.cid) delete state.atCounter[k];
        });
        state.atCounter[w] = a.cid;
        a.worker = w;
        a.phase = a.phase === "asking" ? "asking" : "to-counter";
        a.tx = serveX(w);
        a.ty = SERVE_FEET;
        a.turn = num(ev.turn) || a.turn;
        a.turns = num(ev.turns) || a.turns;
        if (ev.tid) {
          startTurn(ev, a);
          markTurn(ev.tid, { worker: w, waited: num(ev.waited) || 0 });
        }
        return;
      }

      case "step": {
        var sp = state.people[String(ev.cid)];
        var sw = num(ev.worker);
        if (sw != null) {
          if (ev.kind === "llm.start") {
            state.active[sw] = { kind: "llm", name: "llm", since: nowSec() };
          } else if (ev.kind === "tool.start") {
            state.active[sw] = { kind: ev.name, name: ev.name, since: nowSec() };
          } else if (ev.kind === "llm" || ev.kind === "tool") {
            delete state.active[sw];
          }
        }
        if (ev.kind === "tool" && ev.name === "guide") {
          state.flashes.push({ born: nowSec(), rows: (ev.guide || {}).rows || 0 });
        }
        if (ev.kind === "tool" && ev.name === "buy") {
          var b = ev.buy || {};
          if (b.status === "sold" && sw != null) {
            state.bags.push({ worker: sw, born: nowSec(), cid: String(ev.cid) });
            if (sp) sp.bought = true;
          } else if (sw != null) {
            state.gaps.push({ worker: sw, born: nowSec() });
          }
        }
        addStep(ev);
        return;
      }

      case "reply": {
        var r = state.people[String(ev.cid)];
        var rw = num(ev.worker);
        if (rw == null) rw = r ? r.worker : 0;
        if (r) {
          r.worker = rw;
          r.reply = String(ev.text == null ? "" : ev.text);
          r.status = String(ev.status || "");
          r.phase = "served";
          state.atCounter[rw] = r.cid;
        }
        if (rw != null) delete state.active[rw];
        finishTurn(ev);
        return;
      }

      case "leave": {
        var g = state.people[String(ev.cid)];
        if (!g) return;
        g.phase = "leave";
        Object.keys(state.atCounter).forEach(function (k) {
          if (state.atCounter[k] === g.cid) delete state.atCounter[k];
        });
        return;
      }

      case "drop": {
        var d = state.people[String(ev.cid)];
        if (d) { d.phase = "leave"; d.status = "turned"; d.reply = ""; }
        state.dropped = (state.dropped || 0) + 1;
        paintStrip();
        return;
      }

      case "queue":
        state.depth = num(ev.depth) || 0;
        if (num(ev.cap)) state.cap = num(ev.cap);
        if (num(ev.dropped) != null) state.dropped = num(ev.dropped) || 0;
        state.busy = Array.isArray(ev.busy) ? ev.busy : [];
        paintStrip();
        return;

      case "stats":
        ["turns", "sold", "out", "guide", "error", "guides", "buys", "tokens"]
          .forEach(function (k) {
            if (num(ev[k]) != null) state.tally[k] = num(ev[k]);
          });
        if (num(ev.dropped) != null) state.dropped = num(ev.dropped);
        paintStrip();
        return;

      case "metrics": {
        var t = nowSec();
        var tok = num(ev.tokens_per_min);
        var p95 = num(ev.p95);
        if (tok != null) { state.tokNow = tok; state.tokHist.push({ t: t, v: tok }); }
        if (p95 != null) { state.latNow = p95; state.latHist.push({ t: t, v: p95 }); }
        state.source = ev.source || "";
        state.tokHist = state.tokHist.filter(function (x) { return t - x.t < 180; });
        state.latHist = state.latHist.filter(function (x) { return t - x.t < 180; });
        paintStrip();
        return;
      }
    }
  }

  function ingest(raw) {
    try { applyEvent(typeof raw === "string" ? JSON.parse(raw) : raw); }
    catch (err) { /* ignore malformed payloads */ }
  }

  // ---------- the rail ----------

  function startTurn(ev, p) {
    var tid = String(ev.tid);
    if (state.turns[tid]) return state.turns[tid];

    var card = document.createElement("article");
    card.className = "turn";
    card.dataset.status = "open";

    var head = document.createElement("header");
    head.innerHTML =
      '<span class="cid"></span><span class="w"></span>' +
      '<span class="thread"></span><span class="pill open">open</span>' +
      '<span class="dur">…</span><span class="tok"></span>';
    card.appendChild(head);

    var ask = document.createElement("p");
    ask.className = "ask";
    ask.textContent = cut(ev.text || (p && p.ask) || "", 180);
    card.appendChild(ask);

    var steps = document.createElement("ol");
    steps.className = "steps";
    card.appendChild(steps);

    var rec = {
      tid: tid,
      cid: String(ev.cid),
      el: card,
      head: head,
      askEl: ask,
      stepsEl: steps,
      rows: {},
      byRow: {},
      nextOrder: 0,
      wall: 0,
      total: 0,
      turn: num(ev.turn) || (p && p.turn) || 1,
      turns: num(ev.turns) || (p && p.turns) || 1
    };
    state.turns[tid] = rec;
    state.tidOrder.unshift(tid);

    head.querySelector(".cid").textContent = rec.cid;
    head.querySelector(".thread").textContent =
      rec.turns > 1 ? "line " + rec.turn + "/" + rec.turns : "";

    if (emptyEl && emptyEl.parentNode) emptyEl.remove();
    turnsEl.insertBefore(card, turnsEl.firstChild);

    while (state.tidOrder.length > 40) {
      var old = state.tidOrder.pop();
      var dead = state.turns[old];
      if (dead && dead.el && dead.el.parentNode) dead.el.remove();
      delete state.turns[old];
    }
    return rec;
  }

  function markTurn(tid, fields) {
    var rec = state.turns[String(tid)];
    if (!rec) return;
    if (fields.worker != null) {
      rec.worker = fields.worker;
      rec.head.querySelector(".w").textContent = "W" + fields.worker;
    }
    if (fields.waited) {
      rec.waited = fields.waited;
      if (fields.waited >= 0.5) {
        rec.head.querySelector(".thread").textContent =
          (rec.turns > 1 ? "line " + rec.turn + "/" + rec.turns + " · " : "") +
          "waited " + secs(fields.waited);
      }
    }
  }

  function stepRow(rec, key, kind, label) {
    var li = rec.rows[key];
    if (li) return li;
    li = document.createElement("li");
    li.className = kind;
    li.innerHTML =
      '<span class="k"></span><span class="track"><i></i></span>' +
      '<span class="n"></span><span class="detail"></span>';
    li.querySelector(".k").textContent = label;
    rec.rows[key] = li;
    rec.stepsEl.appendChild(li);
    return li;
  }

  function addStep(ev) {
    var rec = state.turns[String(ev.tid)];
    if (!rec) rec = startTurn(ev, state.people[String(ev.cid)]);
    if (!rec) return;

    var kind = ev.kind;
    var nm = kind === "llm" || kind === "llm.start" ? "llm"
           : kind === "retry" ? "retry" : String(ev.name);
    // The row id comes from the counter and is the same for the start and
    // the finish of one call, so a replayed stream settles rows in place.
    var key = "r" + (ev.row != null ? ev.row : nm + ":" + ev.step);

    if (kind === "retry") {
      var rli = stepRow(rec, key, "retry", "retry");
      rli.querySelector(".n").textContent = "#" + (ev.attempt || 1);
      rli.querySelector(".detail").textContent = cut(ev.why || "", 70);
      rli.querySelector(".track i").style.width = "100%";
      return;
    }

    var li = stepRow(rec, key, nm, nm);

    if (kind === "llm.start" || kind === "tool.start") {
      if (rec.byRow[key]) return;              // already finished; ignore a replay
      li.classList.add("live");
      li.querySelector(".n").textContent = "\u2026";
      li.querySelector(".track i").style.left = "0";
      li.querySelector(".track i").style.width = "14%";
      if (nm !== "llm" && ev.args) {
        var q = ev.args.query || ev.args.sku || "";
        if (q) li.querySelector(".detail").textContent = String(q);
      }
      state.active.seenKey = key;
      return;
    }

    if (kind !== "llm" && kind !== "tool") return;

    li.classList.remove("live");
    var seconds = Number(ev.seconds) || 0;
    rec.byRow[key] = { li: li, seconds: seconds, kind: nm, order: rec.nextOrder++ };

    li.querySelector(".n").textContent = secs(seconds);

    var detail = li.querySelector(".detail");
    if (kind === "llm") {
      var tin = Number(ev.prompt_tokens) || 0;
      var tout = Number(ev.completion_tokens) || 0;
      var bits = [];
      if (tin || tout) bits.push(tin + "\u2192" + tout + " tok");
      if (ev.model) bits.push(shortModel(ev.model));
      if ((ev.wants || []).length) bits.push("calls " + ev.wants.join("+"));
      detail.textContent = bits.join("  \u00b7  ");
    } else if (nm === "guide") {
      var g = ev.guide || {};
      var q2 = (ev.args || {}).query || "";
      detail.innerHTML =
        (q2 ? '<b>"' + esc(q2) + '"</b> \u2192 ' : "") +
        (g.rows || 0) + " rows, " + (g.in_stock || 0) + " in stock" +
        (ev.waited > 0.002 ? " \u00b7 waited " + secs(ev.waited) + " on the pipe" : "");
    } else if (nm === "buy") {
      var b = ev.buy || {};
      var ok = b.status === "sold";
      detail.innerHTML =
        '<b class="' + (ok ? "sold" : "out") + '">' + esc(b.status || "?") + "</b>" +
        (b.sku ? " \u00b7 " + esc(b.sku) : "") +
        (b.qty ? " \u00d7" + b.qty : "") +
        (b.price ? " \u00b7 \u20b9" + esc(b.price) : "") +
        (ok ? " \u00b7 " + b.stock_left + " left" : "");
    }

    layoutSteps(rec);
  }

  function layoutSteps(rec) {
    var rows = [];
    Object.keys(rec.byRow).forEach(function (k) { rows.push(rec.byRow[k]); });
    rows.sort(function (a, b) { return a.order - b.order; });
    var total = 0;
    rows.forEach(function (r) { total += r.seconds; });
    rec.total = total;
    var scale = Math.max(total, rec.wall || 0) || 1;
    var at = 0;
    rows.forEach(function (r) {
      var i = r.li.querySelector(".track i");
      i.style.left = ((at / scale) * 100).toFixed(2) + "%";
      i.style.width = Math.max(1.5, (r.seconds / scale) * 100).toFixed(2) + "%";
      at += r.seconds;
    });
  }

  function finishTurn(ev) {
    var rec = state.turns[String(ev.tid)];
    if (!rec) rec = startTurn(ev, state.people[String(ev.cid)]);
    if (!rec) return;
    Object.keys(rec.rows).forEach(function (k) {
      rec.rows[k].classList.remove("live");
    });

    var status = String(ev.status || "guide");
    rec.el.dataset.status = status;
    var pill = rec.head.querySelector(".pill");
    pill.className = "pill " + status;
    pill.textContent = status;
    rec.head.querySelector(".dur").textContent = secs(ev.seconds);
    var tok = Number(ev.tokens) || 0;
    rec.head.querySelector(".tok").textContent = tok ? compact(tok) + " tok" : "";

    // Scale the waterfall against the wall clock of the turn, so the slice
    // nobody is accounting for stays visible as the gap on the right.
    rec.wall = Number(ev.seconds) || 0;
    layoutSteps(rec);

    if (!rec.sayEl) {
      rec.sayEl = document.createElement("p");
      rec.sayEl.className = "say";
      rec.el.appendChild(rec.sayEl);
    }
    rec.sayEl.textContent = cut(ev.text || "", 320);

    var served = (ev.served || []).filter(Boolean);
    if (served.length && !rec.servedEl) {
      rec.servedEl = document.createElement("div");
      rec.servedEl.className = "served";
      rec.el.appendChild(rec.servedEl);
    }
    if (served.length) {
      var seen = {};
      rec.servedEl.innerHTML = "";
      served.forEach(function (m) {
        if (seen[m]) return;
        seen[m] = 1;
        var s = document.createElement("span");
        s.textContent = shortModel(m);
        rec.servedEl.appendChild(s);
      });
    }
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // ---------- metric strip ----------

  var strip = {
    lat: document.getElementById("lat"),
    latSrc: document.getElementById("lat-src"),
    latSpark: document.getElementById("lat-spark"),
    tok: document.getElementById("tok"),
    tokSpark: document.getElementById("tok-spark"),
    depth: document.getElementById("depth"),
    drop: document.getElementById("drop"),
    pips: document.getElementById("pips"),
    mix: document.getElementById("mix"),
    turnsN: document.getElementById("turns-n"),
    calls: document.getElementById("calls"),
    buys: document.getElementById("buys"),
    shelfNote: document.getElementById("shelf-note")
  };

  function spark(svg, hist, color, fill) {
    var pts = hist.slice(-48);
    if (!pts.length) { svg.innerHTML = ""; return; }
    var max = 1;
    pts.forEach(function (p) { max = Math.max(max, p.v); });
    var step = pts.length > 1 ? 100 / (pts.length - 1) : 100;
    var d = "", area = "";
    pts.forEach(function (p, i) {
      var x = (i * step).toFixed(2);
      var y = (23 - (p.v / max) * 21).toFixed(2);
      d += (i ? "L" : "M") + x + " " + y;
    });
    area = d + "L100 24L0 24Z";
    svg.innerHTML =
      '<path d="' + area + '" fill="' + fill + '"/>' +
      '<path d="' + d + '" fill="none" stroke="' + color +
      '" stroke-width="1.4" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
  }

  function paintStrip() {
    strip.lat.innerHTML = (state.latNow || 0).toFixed(1) + "<s>s</s>";
    strip.latSrc.textContent = state.source === "phoenix" ? "phoenix"
      : state.source ? "this floor" : "—";
    spark(strip.latSpark, state.latHist, "#35c98b", "#35c98b1c");

    strip.tok.textContent = compact(state.tokNow || 0);
    spark(strip.tokSpark, state.tokHist, "#4d9bf0", "#4d9bf01c");

    var cap = state.cap || 8;
    strip.depth.innerHTML = state.depth + "<s>/" + cap + " waiting</s>";
    strip.drop.textContent = (state.dropped || 0) + " turned away";
    if (strip.pips.childElementCount !== cap) {
      strip.pips.innerHTML = "";
      for (var i = 0; i < cap; i++) strip.pips.appendChild(document.createElement("i"));
    }
    for (var j = 0; j < cap; j++) {
      var pip = strip.pips.children[j];
      pip.className = j < state.depth
        ? (state.depth >= cap ? "full" : state.depth > cap * 0.6 ? "hot" : "on")
        : "";
    }

    var t = state.tally;
    var worst = Math.max(1, t.sold, t.guide, t.out);
    var rows = strip.mix.children;
    [["sold", t.sold], ["guide", t.guide], ["out", t.out]].forEach(function (pair, i) {
      var row = rows[i];
      if (!row) return;
      row.querySelector("b").style.width = ((pair[1] / worst) * 100).toFixed(1) + "%";
      row.children[2].textContent = String(pair[1]);
    });
    strip.turnsN.textContent = t.turns + " turns";

    strip.calls.innerHTML = compact(t.guides) + "<s>guide</s> " +
      '<span style="color:var(--buy)">' + compact(t.buys) + "</span><s>buy</s>";
    strip.shelfNote.textContent = t.tokens
      ? compact(t.tokens) + " tokens this session"
      : "the shelf is the only writer";
  }

  // ---------- motion ----------

  function updateMotion(dt) {
    var waiting = waitingIds();
    waiting.forEach(function (cid, i) {
      var p = state.people[cid];
      p.tx = queueX(Math.min(i, MAX_VISIBLE - 1));
      p.ty = QUEUE_FEET;
    });

    Object.keys(state.atCounter).forEach(function (w) {
      if (Number(w) >= staffN()) return;
      var p = state.people[state.atCounter[w]];
      if (!p || p.phase === "leave") return;
      p.tx = serveX(Number(w));
      p.ty = SERVE_FEET;
    });

    var t = nowSec();
    state.order.slice().forEach(function (cid) {
      var p = state.people[cid];
      if (!p) return;
      if (p.phase === "leave") {
        p.x -= WALK * dt * 1.25;
        p.step += dt * 9;
        if (p.x < -24) dropPerson(cid);
        return;
      }
      if (p.tx !== p.lastTx || p.ty !== p.lastTy) {
        p.lastTx = p.tx;
        p.lastTy = p.ty;
        p.walkSince = t;
      }
      var dx = p.tx - p.x, dy = p.ty - p.y;
      var stepX = WALK * dt;
      var moving = Math.abs(dx) > 0.6 || Math.abs(dy) > 0.6;
      // A throttled tab or a slow frame must not leave someone stranded in
      // the doorway while their bubble is already over the counter.
      if (moving && t - (p.walkSince || t) > 5) {
        p.x = p.tx;
        p.y = p.ty;
        moving = false;
      } else {
        if (Math.abs(dx) <= stepX) p.x = p.tx; else p.x += Math.sign(dx) * stepX;
        if (Math.abs(dy) <= stepX) p.y = p.ty; else p.y += Math.sign(dy) * stepX;
      }
      p.step += dt * (moving ? 9 : 1.6);
      p.moving = moving;
      if (!moving && p.phase === "to-counter") p.phase = "asking";
    });

    state.bags = state.bags.filter(function (b) { return t - b.born < 2.4; });
    state.flashes = state.flashes.filter(function (f) { return t - f.born < 0.75; });
    state.gaps = state.gaps.filter(function (g) { return t - g.born < 1.1; });
  }

  // ---------- pixel helpers ----------

  function rect(x, y, w, h, c) {
    ctx.fillStyle = c;
    ctx.fillRect(x | 0, y | 0, Math.max(0, w | 0), Math.max(0, h | 0));
  }

  function shade(hex, mul) {
    var n = parseInt(hex.slice(1), 16);
    var r = clamp(Math.round(((n >> 16) & 255) * mul), 0, 255);
    var g = clamp(Math.round(((n >> 8) & 255) * mul), 0, 255);
    var b = clamp(Math.round((n & 255) * mul), 0, 255);
    return "rgb(" + r + "," + g + "," + b + ")";
  }

  // A person, feet centred on (x, y). 14 wide, 56 tall.
  function drawPerson(x, y, opts) {
    x = Math.round(x); y = Math.round(y);
    var shirt = opts.shirt || "#5f74ad";
    var dir = opts.facing === "left" ? -1 : 1;
    var skin = opts.skin || "#d9a575";
    var hair = opts.hair || "#241a12";
    var pants = opts.pants || "#2b3246";
    var shoe = "#17110c";

    // dx is measured from the body's centre, flipped by facing.
    function b(dx, dy, w, h, c) {
      var left = dir > 0 ? dx : -(dx + w);
      rect(x + left, y + dy, w, h, c);
    }

    var walk = opts.walk ? (Math.floor(opts.step) % 2) : 0;
    var bob = opts.walk ? 0 : (Math.floor(opts.step * 0.7) % 2 === 0 ? 0 : 1);
    var dy0 = -PERSON_H + bob;

    // shadow
    rect(x - 6, y - 1, 12, 2, "rgba(0,0,0,0.33)");

    if (!opts.hideLegs) {
      // legs
      if (walk) {
        b(-4, dy0 + 33, 3, 16, pants);
        b(1, dy0 + 33, 3, 14, pants);
        b(-5, dy0 + 49, 4, 4, shoe);
        b(2, dy0 + 47, 4, 4, shoe);
      } else {
        b(-4, dy0 + 33, 3, 18, pants);
        b(1, dy0 + 33, 3, 18, pants);
        b(-5, dy0 + 51, 4, 4, shoe);
        b(1, dy0 + 51, 4, 4, shoe);
      }
    }

    // torso
    b(-5, dy0 + 13, 10, 20, shirt);
    b(-5, dy0 + 13, 2, 20, shade(shirt, 0.74));     // far side in shade
    b(3, dy0 + 13, 2, 20, shade(shirt, 1.12));      // lit side
    b(-5, dy0 + 29, 10, 4, pants);                  // hips

    if (opts.apron) {
      b(-4, dy0 + 18, 8, 15, opts.apron);
      b(-4, dy0 + 18, 8, 1, shade(opts.apron, 0.8));
    }

    // arms
    if (opts.arm === "up") {
      b(4, dy0 + 11, 2, 9, shade(shirt, 0.92));
      b(4, dy0 + 8, 3, 3, skin);
    } else if (opts.arm === "out") {
      b(4, dy0 + 17, 6, 2, shade(shirt, 0.92));
      b(9, dy0 + 16, 3, 3, skin);
    } else {
      b(4, dy0 + 14, 2, 13, shade(shirt, 0.92));
      b(4, dy0 + 26, 2, 3, skin);
      b(-6, dy0 + 14, 2, 13, shade(shirt, 0.7));
      b(-6, dy0 + 26, 2, 3, shade(skin, 0.85));
    }

    // head
    b(-4, dy0 + 4, 8, 9, skin);
    b(-4, dy0 + 4, 2, 9, shade(skin, 0.86));
    b(-4, dy0, 9, 5, hair);
    b(-5, dy0 + 3, 2, 4, hair);
    b(4, dy0 + 2, 1, 3, hair);
    b(2, dy0 + 7, 1, 1, "#14100c");                 // eye
    b(-1, dy0 + 11, 4, 2, shade(skin, 0.9));        // neck
    if (opts.bag) {
      b(5, dy0 + 27, 6, 7, "#bd9a63");
      b(5, dy0 + 27, 6, 1, "#d9bb88");
    }
  }

  function shopkeeper(x, y, pose) {
    drawPerson(x, y, {
      shirt: "#39405a",
      apron: "#c8bca2",
      hair: "#1d150e",
      facing: "left",
      step: 0,
      walk: false,
      hideLegs: true,
      arm: pose === "serve" ? "out" : pose === "think" ? "up" : "down"
    });
  }

  // ---------- shelf goods ----------

  var GOODS = ["box", "jar", "bottle", "sack", "tin", "packet"];
  var GOOD_COLORS = ["#b8703f", "#8a5530", "#cdbb95", "#5f8a5f", "#a4514a",
                     "#b99a60", "#4e7f96", "#9a6a46"];

  function good(kind, x, y, color) {
    switch (kind) {
      case "jar":
        rect(x + 2, y + 2, 6, 3, "#8c9eb0");
        rect(x + 1, y + 5, 8, 9, "#cfe4dd");
        rect(x + 2, y + 8, 6, 5, color);
        rect(x + 1, y + 5, 2, 9, "#e6f2ee");
        break;
      case "bottle":
        rect(x + 4, y, 2, 4, "#7f8c9b");
        rect(x + 3, y + 4, 4, 10, color);
        rect(x + 3, y + 4, 1, 10, shade(color, 1.3));
        break;
      case "sack":
        rect(x + 1, y + 4, 8, 10, color);
        rect(x + 2, y + 2, 6, 3, shade(color, 0.8));
        rect(x + 1, y + 4, 2, 10, shade(color, 1.15));
        break;
      case "tin":
        rect(x + 1, y + 3, 8, 11, color);
        rect(x + 1, y + 3, 8, 2, shade(color, 1.3));
        rect(x + 1, y + 9, 8, 2, shade(color, 0.72));
        break;
      case "packet":
        rect(x + 1, y + 5, 8, 9, color);
        rect(x + 1, y + 5, 8, 1, shade(color, 1.35));
        rect(x + 3, y + 8, 4, 3, shade(color, 0.7));
        break;
      default:
        rect(x, y + 3, 10, 11, color);
        rect(x, y + 3, 10, 2, shade(color, 1.25));
        rect(x, y + 12, 10, 2, shade(color, 0.7));
        rect(x + 3, y + 6, 4, 3, shade(color, 0.78));
    }
  }

  // ---------- the room ----------

  function drawRoom() {
    // wall
    rect(0, 0, W, WALL_H, "#3a2a1f");
    for (var s = 0; s < WALL_H; s += 4) {
      rect(0, s, W, 1, "rgba(0,0,0,0.07)");
    }
    rect(0, 0, W, 7, "#241812");

    // floor
    rect(0, WALL_H, W, H - WALL_H, "#33302c");
    for (var fy = WALL_H + 5; fy < H; fy += 7) {
      rect(0, fy, W, 1, "rgba(0,0,0,0.13)");
    }
    for (var fx = 0; fx < W; fx += 28) {
      rect(fx, WALL_H, 1, H - WALL_H, "rgba(0,0,0,0.08)");
    }
    rect(0, WALL_H, W, 2, "#1d1a17");

    // window, left of the shelving
    rect(8, 18, 40, 44, "#55391f");
    rect(11, 21, 34, 38, "#39526b");
    rect(11, 21, 34, 14, "#486881");
    rect(27, 21, 2, 38, "#55391f");
    rect(11, 38, 34, 2, "#55391f");

    // shelves
    for (var i = 0; i < SHELF_BOARDS.length; i++) {
      var by = SHELF_BOARDS[i];
      var x0 = 56;
      var x1 = W - 8;
      // back panel
      rect(x0, by - 20, x1 - x0, 20, "#2d2017");
      for (var g = x0 + 2, k = 0; g < x1 - 10; g += 13, k++) {
        var kind = GOODS[(k + i * 3) % GOODS.length];
        var color = GOOD_COLORS[(k * 5 + i * 2) % GOOD_COLORS.length];
        good(kind, g, by - 15, color);
      }
      rect(x0, by, x1 - x0, 3, "#6b4830");
      rect(x0, by + 3, x1 - x0, 2, "#2a1b11");
    }

    // price board on the wall between the shelves and the counter
    rect(W - 104, SIGN_Y, 96, 32, "#1b2a22");
    rect(W - 104, SIGN_Y, 96, 2, "#2e4a3c");
    for (var r = 0; r < 4; r++) {
      rect(W - 98, SIGN_Y + 6 + r * 7, 44 - (r % 3) * 7, 2, "#49705d");
      rect(W - 44, SIGN_Y + 6 + r * 7, 14, 2, "#6c9a81");
    }

    // hanging lamps
    [84, 214, 344].forEach(function (lx) {
      rect(lx, 0, 1, 12, "#4a3a28");
      rect(lx - 5, 12, 11, 3, "#1f1a14");
      rect(lx - 4, 15, 9, 2, "#f2c870");
      ctx.fillStyle = "rgba(246, 204, 126, 0.07)";
      ctx.beginPath();
      ctx.moveTo(lx - 4, 17);
      ctx.lineTo(lx + 5, 17);
      ctx.lineTo(lx + 26, WALL_H);
      ctx.lineTo(lx - 25, WALL_H);
      ctx.closePath();
      ctx.fill();
    });

    // guide sweep: the shelf being read
    var t = nowSec();
    state.flashes.forEach(function (f) {
      var k = (t - f.born) / 0.75;
      var sx = 56 + k * (W - 64);
      var top = SHELF_BOARDS[0] - 20;
      var tall = SHELF_BOARDS[1] - SHELF_BOARDS[0] + 20;
      var glow = ctx.createLinearGradient(sx - 10, 0, sx + 10, 0);
      var peak = (0.17 * (1 - k)).toFixed(3);
      glow.addColorStop(0, "rgba(240, 196, 118, 0)");
      glow.addColorStop(0.5, "rgba(240, 196, 118, " + peak + ")");
      glow.addColorStop(1, "rgba(240, 196, 118, 0)");
      ctx.fillStyle = glow;
      ctx.fillRect(sx - 10, top, 20, tall);
    });

    // doorway, our side of the room
    rect(DOOR_X, DOOR_Y, DOOR_W, DOOR_H, "#211812");
    rect(DOOR_X, DOOR_Y, DOOR_W, 3, "#4b3524");
    rect(DOOR_X + DOOR_W - 3, DOOR_Y, 3, DOOR_H, "#4b3524");
    rect(DOOR_X + 3, DOOR_Y + 4, DOOR_W - 7, 22, "#2b4254");
    rect(DOOR_X + 3, DOOR_Y + 30, DOOR_W - 7, DOOR_H - 31, "#191410");

    // plant in the right corner
    rect(W - 24, 150, 16, 20, "#4b3425");
    rect(W - 22, 144, 12, 8, "#3b6a46");
    rect(W - 19, 136, 7, 10, "#49814f");
  }

  function drawCounter() {
    var y = COUNTER_TOP;
    rect(COUNTER_X, y, COUNTER_W, SURF_H, "#c48a52");
    rect(COUNTER_X, y, COUNTER_W, 2, "#ecc08c");
    rect(COUNTER_X, y + SURF_H, COUNTER_W, FRONT_H, "#78492a");
    rect(COUNTER_X, y + SURF_H, COUNTER_W, 2, "#5a3620");
    for (var p = 0; p < COUNTER_W - 20; p += 46) {
      rect(COUNTER_X + 10 + p, y + SURF_H + 5, 30, 12, "#6b4126");
      rect(COUNTER_X + 10 + p, y + SURF_H + 5, 30, 1, "#8a5733");
    }
    var legY = y + SURF_H + FRONT_H;
    for (var l = 0; l < 5; l++) {
      var lx = COUNTER_X + 12 + l * ((COUNTER_W - 32) / 4);
      rect(lx, legY, 7, LEG_H, "#432a18");
    }

    // the weighing scale, so the counter is not an empty plank
    rect(COUNTER_X + 8, y - 9, 14, 9, "#8d94a0");
    rect(COUNTER_X + 10, y - 12, 10, 3, "#aeb6c2");
    rect(COUNTER_X + 12, y - 7, 6, 3, "#55606e");
  }

  function drawPeople() {
    var n = staffN();
    var t = nowSec();

    // served customers sit behind the counter front, so it covers their legs
    for (var w = 0; w < n; w++) {
      var cid = state.atCounter[w];
      var p = cid && state.people[cid];
      if (!p || p.phase === "leave") continue;
      drawPerson(p.x, p.y, {
        shirt: p.shirt, facing: "right", step: p.step,
        walk: p.moving, bag: p.bought
      });
    }

    // shopkeepers behind the counter
    for (var i = 0; i < n; i++) {
      var act = state.active[i];
      var sid = state.atCounter[i];
      var pose = "lean";
      if (act && act.kind !== "llm") pose = "serve";
      else if (act) pose = "think";
      else if (sid && state.people[sid]) pose = "serve";
      shopkeeper(keeperX(i), STAFF_FEET, pose);
    }

    drawCounter();

    // what landed on the counter
    state.bags.forEach(function (b) {
      if (b.worker >= n) return;
      var age = t - b.born;
      var bx = keeperX(b.worker) + 4 + Math.min(20, age * 14);
      rect(bx, COUNTER_TOP - 10, 11, 10, "#bd9a63");
      rect(bx, COUNTER_TOP - 10, 11, 2, "#d9bb88");
      rect(bx + 3, COUNTER_TOP - 13, 2, 4, "#8a6a3a");
      rect(bx + 7, COUNTER_TOP - 13, 2, 4, "#8a6a3a");
    });
    state.gaps.forEach(function (g) {
      if (g.worker >= n) return;
      if (Math.floor((t - g.born) * 7) % 2) return;
      var gx = keeperX(g.worker) + 2;
      rect(gx, COUNTER_TOP - 8, 12, 8, "#3a2b22");
      rect(gx + 1, COUNTER_TOP - 7, 10, 6, "#271c16");
    });

    // the line, in front of everything
    var waiting = waitingIds();
    waiting.slice(0, MAX_VISIBLE).forEach(function (cid) {
      var p = state.people[cid];
      drawPerson(p.x, p.y, {
        shirt: p.shirt, facing: "right", step: p.step, walk: p.moving
      });
    });

    // people walking out
    state.order.forEach(function (cid) {
      var p = state.people[cid];
      if (!p || p.phase !== "leave") return;
      drawPerson(p.x, p.y, {
        shirt: p.shirt, facing: "left", step: p.step, walk: true, bag: p.bought
      });
    });
  }

  // ---------- overlay (all the words) ----------

  var els = {};

  function want(key, cls, x, y) {
    var el = els[key];
    if (!el) {
      el = document.createElement("div");
      el.className = cls;
      overlay.appendChild(el);
      els[key] = el;
    }
    el.dataset.keep = "1";
    if (el.className !== cls) el.className = cls;
    el._nx = x;                                   // native x of the speaker
    var tp = ((y / H) * 100).toFixed(3) + "%";
    if (el.style.top !== tp) el.style.top = tp;
    return el;
  }

  // A bubble at the edge of the room would hang off the picture. Nudge the
  // box back inside and slide its tail the other way, so it still points at
  // whoever is talking.
  function placeOverlay() {
    var ow = overlay.clientWidth;
    if (!ow) return;
    Object.keys(els).forEach(function (k) {
      var el = els[k];
      if (el._nx == null) return;
      var anchor = (el._nx / W) * ow;
      var half = el.offsetWidth / 2;
      var centre = clamp(anchor, half + 3, ow - half - 3);
      var left = centre.toFixed(1) + "px";
      if (el.style.left !== left) el.style.left = left;
      var tail = (anchor - centre).toFixed(1) + "px";
      if (el._tail !== tail) {
        el.style.setProperty("--tail", tail);
        el._tail = tail;
      }
    });
  }

  function setHTML(el, html) {
    if (el._h !== html) { el.innerHTML = html; el._h = html; }
  }

  function syncOverlay() {
    Object.keys(els).forEach(function (k) { delete els[k].dataset.keep; });
    var n = staffN();

    for (var i = 0; i < state.workers; i++) {
      var off = i >= n;
      var busy = !off && state.busy.indexOf(i) >= 0;
      var plate = want("plate" + i, "plate" + (off ? " off" : busy ? " busy" : ""),
                       workerX(i), COUNTER_TOP + SURF_H + FRONT_H + LEG_H + 3);
      setHTML(plate, '<span class="dot">●</span> W' + i +
              (off ? " off" : busy ? "" : " idle"));

      var act = state.active[i];
      if (!off && act) {
        var el = Math.max(0, nowSec() - act.since);
        var chip = want("chip" + i, "chip " + (act.kind === "llm" ? "llm" : act.name),
                        keeperX(i), STAFF_FEET - PERSON_H - 4);
        setHTML(chip, (act.kind === "llm" ? "thinking" : act.name) +
                '<span class="el">' + el.toFixed(1) + "s</span>");
      }
    }

    for (var w = 0; w < n; w++) {
      var cid = state.atCounter[w];
      var p = cid && state.people[cid];
      if (!p || p.phase === "leave") continue;

      if (p.ask) {
        var ask = want("ask" + p.cid, "bub ask", p.x, p.y - PERSON_H - 5);
        setHTML(ask,
          (p.turns > 1 ? '<span class="turnof">line ' + p.turn + " of " + p.turns + "</span>" : "") +
          esc(cut(p.ask, 110)));
      }
      if (p.reply) {
        var say = want("say" + p.cid, "bub say " + (p.status || ""),
                       keeperX(w), STAFF_FEET - PERSON_H - 26);
        setHTML(say, esc(cut(p.reply, 190)));
      }
    }

    var waiting = waitingIds();
    waiting.slice(0, 4).forEach(function (cid, i) {
      var p = state.people[cid];
      if (!p.ask) return;
      var tag = want("q" + p.cid, "queuetag", p.x, p.y - PERSON_H - 3 - (i % 2) * 12);
      setHTML(tag, esc(cut(p.ask, 22)));
    });

    var extra = Math.max(0, waiting.length - MAX_VISIBLE);
    if (extra > 0) {
      var more = want("more", "queuetag", DOOR_X + DOOR_W / 2, DOOR_Y - 4);
      setHTML(more, "+" + extra + " outside");
    }

    Object.keys(els).forEach(function (k) {
      if (els[k].dataset.keep) return;
      els[k].remove();
      delete els[k];
    });

    placeOverlay();
  }

  // ---------- fit ----------

  var fitW = 0, fitH = 0;

  function fit() {
    var aw = stage.clientWidth - 16;
    var ah = stage.clientHeight - 16;
    if (aw <= 0 || ah <= 0) return;
    var raw = Math.min(aw / W, ah / H);
    // Quarter steps above 2x: close enough to the grid to stay crisp, near
    // enough to the box that the room is not swimming in empty panel.
    var s = raw >= 2 ? Math.floor(raw * 4) / 4 : raw;
    var px = Math.round(W * s);
    var py = Math.round(H * s);
    if (px === fitW && py === fitH) return;
    fitW = px; fitH = py;
    canvas.style.width = px + "px";
    canvas.style.height = py + "px";
    frame.style.width = px + "px";
    frame.style.height = py + "px";
  }

  window.addEventListener("resize", fit);
  if (window.ResizeObserver) new ResizeObserver(fit).observe(stage);

  // ---------- connect ----------

  function setLive(mode, text) {
    liveEl.dataset.mode = mode;
    liveText.textContent = text;
  }

  function connect() {
    if (location.protocol !== "http:" && location.protocol !== "https:") {
      startTape();
      return;
    }
    var source, opened = false;
    try { source = new EventSource("/events"); }
    catch (err) { startTape(); return; }
    source.onopen = function () { opened = true; setLive("live", "live"); };
    source.onmessage = function (msg) {
      if (!opened) { opened = true; setLive("live", "live"); }
      ingest(msg.data);
    };
    source.onerror = function () {
      // A drop after a good open keeps the picture and lets EventSource
      // retry. A failure before any open means there is no server.
      if (opened) { setLive("down", "reconnecting"); return; }
      try { source.close(); } catch (e) {}
      startTape();
    };
  }

  // ---------- tape: opened as a file, or no server ----------

  var TAPE = (function () {
    var t = [];
    function at(time, ev) { t.push({ at: time, ev: ev }); }

    at(0.1, { type: "hello", staff: 2, arrival: 4, workers: 3, cap: 8,
              model: "openrouter/free", conversations: 100 });

    // c1 — a two line conversation that ends in a sale
    at(0.5, { type: "arrive", cid: "4b1c9a02", tid: null, text: "A bag of rice, please.", turn: 1, turns: 2, queued: true });
    at(0.9, { type: "arrive", cid: "7e33f1aa", tid: null, text: "What tea do you have?", turn: 1, turns: 2, queued: true });
    at(1.3, { type: "arrive", cid: "9ac2d510", tid: null, text: "Soap, toothpaste, and a sachet of shampoo.", turn: 1, turns: 1, queued: true });
    at(1.5, { type: "queue", depth: 3, cap: 8, busy: [], dropped: 0 });

    at(1.7, { type: "assign", cid: "4b1c9a02", tid: "t1", worker: 0, turn: 1, turns: 2, waited: 1.2 });
    at(1.8, { type: "queue", depth: 2, cap: 8, busy: [0], dropped: 0 });
    at(1.9, { type: "step", cid: "4b1c9a02", tid: "t1", worker: 0, kind: "llm.start", step: 1, model: "openrouter/free" });
    at(3.8, { type: "step", cid: "4b1c9a02", tid: "t1", worker: 0, kind: "llm", step: 1, model: "qwen/qwen-2.5-72b-instruct:free", seconds: 1.86, prompt_tokens: 712, completion_tokens: 28, wants: ["guide"] });
    at(3.85, { type: "step", cid: "4b1c9a02", tid: "t1", worker: 0, kind: "tool.start", step: 1, name: "guide", args: { query: "rice" } });
    at(3.92, { type: "step", cid: "4b1c9a02", tid: "t1", worker: 0, kind: "tool", step: 1, name: "guide", args: { query: "rice" }, seconds: 0.041, waited: 0.004, guide: { rows: 8, in_stock: 7, offered: [] } });
    at(4.0, { type: "step", cid: "4b1c9a02", tid: "t1", worker: 0, kind: "llm.start", step: 2, model: "openrouter/free" });
    at(6.1, { type: "step", cid: "4b1c9a02", tid: "t1", worker: 0, kind: "llm", step: 2, model: "qwen/qwen-2.5-72b-instruct:free", seconds: 2.08, prompt_tokens: 1180, completion_tokens: 64, wants: [] });
    at(6.2, { type: "reply", cid: "4b1c9a02", tid: "t1", worker: 0, text: "Rice hai — 1 kg ₹68, 5 kg ₹310. Kaunsa doon?", status: "guide", tokens: 1984, seconds: 4.3, served: ["qwen/qwen-2.5-72b-instruct:free"], steps: [] });
    at(6.3, { type: "stats", turns: 1, sold: 0, guide: 1, out: 0, error: 0, guides: 1, buys: 0, tokens: 1984 });

    at(1.9, { type: "assign", cid: "7e33f1aa", tid: "t2", worker: 1, turn: 1, turns: 2, waited: 1.0 });
    at(2.0, { type: "queue", depth: 1, cap: 8, busy: [0, 1], dropped: 0 });
    at(2.1, { type: "step", cid: "7e33f1aa", tid: "t2", worker: 1, kind: "llm.start", step: 1, model: "openrouter/free" });
    at(4.9, { type: "step", cid: "7e33f1aa", tid: "t2", worker: 1, kind: "llm", step: 1, model: "meta-llama/llama-3.3-70b-instruct:free", seconds: 2.74, prompt_tokens: 706, completion_tokens: 24, wants: ["guide"] });
    at(4.95, { type: "step", cid: "7e33f1aa", tid: "t2", worker: 1, kind: "tool.start", step: 1, name: "guide", args: { query: "tea" } });
    at(5.12, { type: "step", cid: "7e33f1aa", tid: "t2", worker: 1, kind: "tool", step: 1, name: "guide", args: { query: "tea" }, seconds: 0.038, waited: 0.112, guide: { rows: 11, in_stock: 9, offered: [] } });
    at(5.2, { type: "step", cid: "7e33f1aa", tid: "t2", worker: 1, kind: "llm.start", step: 2, model: "openrouter/free" });
    at(7.6, { type: "step", cid: "7e33f1aa", tid: "t2", worker: 1, kind: "llm", step: 2, model: "meta-llama/llama-3.3-70b-instruct:free", seconds: 2.33, prompt_tokens: 1402, completion_tokens: 88, wants: [] });
    at(7.7, { type: "reply", cid: "7e33f1aa", tid: "t2", worker: 1, text: "Dust chai ₹42 (250 g), Red Label ₹68, green tea ₹115. Dust sabse sasta hai.", status: "guide", tokens: 2220, seconds: 5.6, served: ["meta-llama/llama-3.3-70b-instruct:free"], steps: [] });

    at(8.0, { type: "metrics", tokens_per_min: 4204, p95: 5.6, source: "phoenix" });

    // follow-ups: same worker, same memory, a real sale
    at(8.4, { type: "arrive", cid: "4b1c9a02", tid: "t3", text: "The 5 kg, if you have it.", turn: 2, turns: 2, queued: false });
    at(8.5, { type: "assign", cid: "4b1c9a02", tid: "t3", worker: 0, turn: 2, turns: 2, waited: 0 });
    at(8.6, { type: "step", cid: "4b1c9a02", tid: "t3", worker: 0, kind: "llm.start", step: 1, model: "openrouter/free" });
    at(10.5, { type: "step", cid: "4b1c9a02", tid: "t3", worker: 0, kind: "llm", step: 1, model: "qwen/qwen-2.5-72b-instruct:free", seconds: 1.91, prompt_tokens: 1310, completion_tokens: 42, wants: ["buy"] });
    at(10.55, { type: "step", cid: "4b1c9a02", tid: "t3", worker: 0, kind: "tool.start", step: 1, name: "buy", args: { sku: "RIC-5K-IND", qty: 1 } });
    at(10.68, { type: "step", cid: "4b1c9a02", tid: "t3", worker: 0, kind: "tool", step: 1, name: "buy", args: { sku: "RIC-5K-IND", qty: 1 }, seconds: 0.062, waited: 0.008, buy: { status: "sold", sku: "RIC-5K-IND", qty: 1, price: "310.00", stock_left: 7 } });
    at(10.8, { type: "step", cid: "4b1c9a02", tid: "t3", worker: 0, kind: "llm.start", step: 2, model: "openrouter/free" });
    at(12.4, { type: "step", cid: "4b1c9a02", tid: "t3", worker: 0, kind: "llm", step: 2, model: "qwen/qwen-2.5-72b-instruct:free", seconds: 1.58, prompt_tokens: 1498, completion_tokens: 52, wants: [] });
    at(12.5, { type: "reply", cid: "4b1c9a02", tid: "t3", worker: 0, text: "Ho gaya — 5 kg India Gate, ₹310. Saat bag bache hain.", status: "sold", tokens: 2902, seconds: 3.9, served: ["qwen/qwen-2.5-72b-instruct:free"], steps: [] });
    at(12.6, { type: "stats", turns: 3, sold: 1, guide: 2, out: 0, error: 0, guides: 2, buys: 1, tokens: 7106 });
    at(14.2, { type: "leave", cid: "4b1c9a02" });
    at(14.3, { type: "queue", depth: 1, cap: 8, busy: [1], dropped: 0 });

    // an out of stock path
    at(14.6, { type: "assign", cid: "9ac2d510", tid: "t4", worker: 0, turn: 1, turns: 1, waited: 13.3 });
    at(14.7, { type: "queue", depth: 0, cap: 8, busy: [0, 1], dropped: 0 });
    at(14.8, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "llm.start", step: 1, model: "openrouter/free" });
    at(17.0, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "llm", step: 1, model: "mistralai/mistral-small-3.2-24b-instruct:free", seconds: 2.18, prompt_tokens: 722, completion_tokens: 31, wants: ["guide"] });
    at(17.1, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "tool.start", step: 1, name: "guide", args: { query: "shampoo" } });
    at(17.3, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "tool", step: 1, name: "guide", args: { query: "shampoo" }, seconds: 0.034, waited: 0.191, guide: { rows: 4, in_stock: 0, offered: [] } });
    at(17.4, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "llm.start", step: 2, model: "openrouter/free" });
    at(19.4, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "llm", step: 2, model: "mistralai/mistral-small-3.2-24b-instruct:free", seconds: 1.94, prompt_tokens: 980, completion_tokens: 36, wants: ["buy"] });
    at(19.45, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "tool.start", step: 2, name: "buy", args: { sku: "SHM-SAC-CLI", qty: 1 } });
    at(19.6, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "tool", step: 2, name: "buy", args: { sku: "SHM-SAC-CLI", qty: 1 }, seconds: 0.048, waited: 0.006, buy: { status: "out_of_stock", sku: "SHM-SAC-CLI", qty: 0, price: "3.00", stock_left: 0 } });
    at(19.7, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "llm.start", step: 3, model: "openrouter/free" });
    at(21.3, { type: "step", cid: "9ac2d510", tid: "t4", worker: 0, kind: "llm", step: 3, model: "mistralai/mistral-small-3.2-24b-instruct:free", seconds: 1.61, prompt_tokens: 1240, completion_tokens: 58, wants: [] });
    at(21.4, { type: "reply", cid: "9ac2d510", tid: "t4", worker: 0, text: "Shampoo sachet khatam hai. Soap aur toothpaste hai — wo de doon?", status: "out", tokens: 3067, seconds: 6.6, served: ["mistralai/mistral-small-3.2-24b-instruct:free"], steps: [] });
    at(21.5, { type: "stats", turns: 4, sold: 1, guide: 2, out: 1, error: 0, guides: 3, buys: 2, tokens: 10173 });
    at(21.6, { type: "metrics", tokens_per_min: 6102, p95: 6.6, source: "phoenix" });

    at(9.0, { type: "arrive", cid: "c71b8e44", tid: null, text: "I need oil for frying. Which one is the better deal?", turn: 1, turns: 3, queued: true });
    at(11.0, { type: "arrive", cid: "2d9f6b17", tid: null, text: "Do you have eggs?", turn: 1, turns: 1, queued: true });
    at(13.0, { type: "arrive", cid: "88ea3c05", tid: null, text: "Basmati rice, one kilo. And salt.", turn: 1, turns: 2, queued: true });
    at(15.0, { type: "arrive", cid: "5f0d7a93", tid: null, text: "Sugar, 1 kg. And if the big pack is cheaper, that one.", turn: 1, turns: 2, queued: true });
    at(16.0, { type: "queue", depth: 4, cap: 8, busy: [0, 1], dropped: 0 });

    at(9.6, { type: "arrive", cid: "7e33f1aa", tid: "t5", text: "The cheaper dust, 250 grams.", turn: 2, turns: 2, queued: false });
    at(9.7, { type: "assign", cid: "7e33f1aa", tid: "t5", worker: 1, turn: 2, turns: 2, waited: 0 });
    at(9.8, { type: "step", cid: "7e33f1aa", tid: "t5", worker: 1, kind: "llm.start", step: 1, model: "openrouter/free" });
    at(12.0, { type: "step", cid: "7e33f1aa", tid: "t5", worker: 1, kind: "llm", step: 1, model: "meta-llama/llama-3.3-70b-instruct:free", seconds: 2.16, prompt_tokens: 1520, completion_tokens: 38, wants: ["buy"] });
    at(12.05, { type: "step", cid: "7e33f1aa", tid: "t5", worker: 1, kind: "tool.start", step: 1, name: "buy", args: { sku: "TEA-250-DUS", qty: 1 } });
    at(12.2, { type: "step", cid: "7e33f1aa", tid: "t5", worker: 1, kind: "tool", step: 1, name: "buy", args: { sku: "TEA-250-DUS", qty: 1 }, seconds: 0.057, waited: 0.021, buy: { status: "sold", sku: "TEA-250-DUS", qty: 1, price: "42.00", stock_left: 14 } });
    at(12.3, { type: "step", cid: "7e33f1aa", tid: "t5", worker: 1, kind: "llm.start", step: 2, model: "openrouter/free" });
    at(14.0, { type: "step", cid: "7e33f1aa", tid: "t5", worker: 1, kind: "llm", step: 2, model: "meta-llama/llama-3.3-70b-instruct:free", seconds: 1.64, prompt_tokens: 1690, completion_tokens: 44, wants: [] });
    at(14.1, { type: "reply", cid: "7e33f1aa", tid: "t5", worker: 1, text: "Dust chai 250 g, ₹42. Rakh di — 14 pack bache.", status: "sold", tokens: 3292, seconds: 4.2, served: ["meta-llama/llama-3.3-70b-instruct:free"], steps: [] });
    at(16.4, { type: "leave", cid: "7e33f1aa" });

    at(16.8, { type: "assign", cid: "c71b8e44", tid: "t6", worker: 1, turn: 1, turns: 3, waited: 7.8 });
    at(16.9, { type: "step", cid: "c71b8e44", tid: "t6", worker: 1, kind: "llm.start", step: 1, model: "openrouter/free" });
    at(19.9, { type: "step", cid: "c71b8e44", tid: "t6", worker: 1, kind: "llm", step: 1, model: "google/gemma-3-27b-it:free", seconds: 2.92, prompt_tokens: 734, completion_tokens: 33, wants: ["guide"] });
    at(20.0, { type: "step", cid: "c71b8e44", tid: "t6", worker: 1, kind: "tool.start", step: 1, name: "guide", args: { query: "oil" } });
    at(20.4, { type: "step", cid: "c71b8e44", tid: "t6", worker: 1, kind: "tool", step: 1, name: "guide", args: { query: "oil" }, seconds: 0.046, waited: 0.308, guide: { rows: 12, in_stock: 10, offered: [] } });
    at(20.5, { type: "step", cid: "c71b8e44", tid: "t6", worker: 1, kind: "llm.start", step: 2, model: "openrouter/free" });
    at(23.6, { type: "step", cid: "c71b8e44", tid: "t6", worker: 1, kind: "llm", step: 2, model: "google/gemma-3-27b-it:free", seconds: 3.04, prompt_tokens: 1612, completion_tokens: 102, wants: [] });
    at(23.7, { type: "reply", cid: "c71b8e44", tid: "t6", worker: 1, text: "1 L sunflower ₹142, 5 L ₹655 — 5 L mein per litre ₹131, wahi sasta.", status: "guide", tokens: 2481, seconds: 6.9, served: ["google/gemma-3-27b-it:free"], steps: [] });
    at(23.8, { type: "stats", turns: 6, sold: 2, guide: 3, out: 1, error: 0, guides: 4, buys: 3, tokens: 15946 });
    at(24.0, { type: "metrics", tokens_per_min: 7340, p95: 6.9, source: "phoenix" });
    at(23.9, { type: "leave", cid: "9ac2d510" });
    at(25.5, { type: "leave", cid: "c71b8e44" });
    at(26.0, { type: "queue", depth: 3, cap: 8, busy: [], dropped: 0 });

    t.sort(function (a, b) { return a.at - b.at; });
    return t;
  })();

  var tapeIndex = 0, tapeStart = 0, usingTape = false;

  function startTape() {
    if (usingTape) return;
    usingTape = true;
    tapeIndex = 0;
    tapeStart = nowSec();
    setLive("tape", "tape · no server");
    modelEl.textContent = "openrouter/free  ·  recorded sample";
  }

  function tickTape() {
    if (!usingTape) return;
    var elapsed = nowSec() - tapeStart;
    while (tapeIndex < TAPE.length && TAPE[tapeIndex].at <= elapsed) {
      applyEvent(TAPE[tapeIndex].ev);
      tapeIndex++;
    }
    if (tapeIndex >= TAPE.length && elapsed > TAPE[TAPE.length - 1].at + 4) {
      Object.keys(state.people).forEach(dropPerson);
      state.bags = []; state.flashes = []; state.gaps = [];
      state.active = {}; state.atCounter = {};
      tapeIndex = 0;
      tapeStart = nowSec();
    }
  }

  // ---------- loop ----------

  var last = nowSec();
  var tickChip = 0;

  function loop() {
    var t = nowSec();
    var dt = Math.min(0.2, t - last);
    last = t;

    fit();
    tickTape();
    updateMotion(dt);

    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, W, H);
    drawRoom();
    drawPeople();

    syncOverlay();

    // the live step clock only needs a few updates a second
    tickChip += dt;
    if (tickChip > 0.2) { tickChip = 0; }

    requestAnimationFrame(loop);
  }

  setSliders(2, 4, true);
  paintStrip();
  fit();
  connect();
  requestAnimationFrame(loop);
})();
