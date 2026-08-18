/* PortBench — leaderboard site.
   Vanilla JS, classic script (no modules, so file:// does not choke on CORS
   for the script itself). All rendering is driven by the JSON under site/data/;
   the only thing hard-coded here is presentation. Swapping real results in is a
   data-file change, never a code change. */

(function () {
  "use strict";

  /* ------------------------------------------------------------------ util */

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var esc = function (s) { return window.PB_HL.escape(s); };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function pct(x, digits) { return (x * 100).toFixed(digits === undefined ? 1 : digits) + "%"; }

  function money(v) {
    if (v === null || !isFinite(v)) return "—";
    return "$" + (v < 1 ? v.toFixed(3) : v.toFixed(2));
  }

  function intFmt(v) { return Math.round(v).toLocaleString("en-US"); }

  /* WCAG relative luminance, used to pick black/white text on a coloured bar. */
  function luminance(hex) {
    var c = hex.replace("#", "");
    var parts = [c.slice(0, 2), c.slice(2, 4), c.slice(4, 6)].map(function (h) {
      var v = parseInt(h, 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2];
  }
  function inkOn(hex) {
    var l = luminance(hex);
    return (l + 0.05) / 0.05 > 1.05 / (l + 0.05) ? "rgba(0,0,0,.78)" : "rgba(255,255,255,.94)";
  }

  /* Round a max up to a readable axis bound with ~5-6 ticks. */
  function niceScale(max, targetTicks) {
    var ticks = targetTicks || 6;
    if (!(max > 0)) return { max: 1, ticks: [0, 1], step: 1 };
    var raw = max / (ticks - 1);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var step = [1, 2, 2.5, 5, 10].reduce(function (best, m) {
      return best === null || (m * mag >= raw && m * mag < best) ? (m * mag >= raw ? m * mag : best) : best;
    }, null) || mag * 10;
    var out = [];
    for (var v = 0; v <= max + 1e-9; v += step) out.push(v);
    if (out[out.length - 1] < max) out.push(out[out.length - 1] + step);
    return { max: out[out.length - 1], ticks: out, step: step };
  }

  /* --------------------------------------------------------------- palette */

  var BUCKET_COLOR = {
    borrow: "#ce422b",
    types: "#dd8b2c",
    imports: "#5b9ec9",
    other_compile: "#7d848f",
    test_fail: "#a273c9",
    harness: "#565d69"
  };

  var VERDICT_COLOR = {
    PASS: "#5aab80",
    COMPILE_ERROR: "#ce422b",
    TEST_FAIL: "#a273c9",
    EXTRACTION_ERROR: "#dd8b2c",
    TRANSPORT_ERROR: "#5b9ec9",
    TIMEOUT: "#565d69"
  };

  /* ------------------------------------------------------------------ data */

  var DATA = {};

  function fetchJSON(url) {
    return fetch(url, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(url + " → HTTP " + r.status);
      return r.json();
    });
  }

  function load() {
    return fetchJSON("data/manifest.json").then(function (man) {
      var keys = Object.keys(man.files);
      return Promise.all(keys.map(function (k) {
        return fetchJSON("data/" + man.files[k]);
      })).then(function (all) {
        var out = { manifest: man };
        keys.forEach(function (k, i) { out[k] = all[i]; });
        return out;
      });
    });
  }

  var MOUNTS = ["#board-mount", "#taxonomy-mount", "#repair-mount", "#gallery-mount", "#trend-mount"];

  function failAll(err) {
    var isFile = location.protocol === "file:";
    MOUNTS.forEach(function (sel, i) {
      var mount = $(sel);
      if (!mount) return;
      mount.className = "errorbox";
      mount.innerHTML = "";
      if (i === 0) {
        var p1 = el("p");
        p1.textContent = isFile
          ? "Could not read the data files. Browsers block fetch() on file:// URLs."
          : "Could not read the data files: " + err.message;
        mount.appendChild(p1);
        var p2 = el("p");
        p2.innerHTML = isFile
          ? "Serve this directory instead — from <code>site/</code> run <code>python3 -m http.server 8000</code> and open <code>http://localhost:8000/</code>."
          : "Check that <code>site/data/manifest.json</code> and the files it points at are deployed alongside <code>index.html</code>.";
        mount.appendChild(p2);
      } else {
        mount.textContent = "Unavailable — see above.";
      }
    });
  }

  /* --------------------------------------------------------- sample banner */

  function renderSampleBanner(data) {
    var flagged = Object.keys(data).filter(function (k) {
      return data[k] && data[k].sample === true;
    });
    if (!flagged.length) return;
    document.body.classList.add("has-sample");
    var banner = $("#sample-banner");
    banner.hidden = false;
    $("#sample-banner-text").textContent =
      "Placeholder numbers for layout only — " + flagged.length + " of " +
      (Object.keys(data).length - 1) + " data files are marked sample. " +
      "Nothing on this page is a measurement yet.";

    /* The nav docks under the banner; the banner wraps to different heights at
       different widths, so measure rather than hard-code an offset. */
    function measure() {
      document.documentElement.style.setProperty("--banner-h", banner.offsetHeight + "px");
    }
    measure();
    var t = null;
    window.addEventListener("resize", function () {
      clearTimeout(t);
      t = setTimeout(measure, 120);
    });
  }

  /* ------------------------------------------------------------ derivation */

  function derive(m) {
    var n = m.tasks_attempted;
    var cost =
      m.tokens.in / 1e6 * m.price_per_mtok_usd.in +
      m.tokens.out / 1e6 * m.price_per_mtok_usd.out;
    return {
      raw: m,
      model: m.model,
      label: m.label,
      family: m.family,
      n: n,
      one: m.passed_oneshot,
      rep: m.passed_after_repair,
      oneRate: m.passed_oneshot / n,
      repRate: m.passed_after_repair / n,
      liftPP: (m.passed_after_repair - m.passed_oneshot) / n * 100,
      recovery: (m.passed_after_repair - m.passed_oneshot) / (n - m.passed_oneshot),
      cost: cost,
      costPerSolved: m.passed_after_repair > 0 ? cost / m.passed_after_repair : null
    };
  }

  /* ------------------------------------------------------------- statstrip */

  function renderStats(lb) {
    var rows = lb.models.map(derive);
    var bestOne = rows.reduce(function (a, b) { return b.oneRate > a.oneRate ? b : a; });
    var bestRep = rows.reduce(function (a, b) { return b.repRate > a.repRate ? b : a; });
    var priced = rows.filter(function (r) { return r.costPerSolved !== null; });
    var cheapest = (priced.length ? priced : rows).reduce(function (a, b) { return b.costPerSolved < a.costPerSolved ? b : a; });
    var stats = [
      { v: String(lb.dataset.tasks_total), k: "tasks · python → rust" },
      { v: String(lb.models.length), k: "models evaluated" },
      { v: String(Object.keys(lb.dataset.projects).length), k: "real crates under test" },
      { v: pct(bestOne.oneRate), k: "best one-shot · " + bestOne.label },
      { v: pct(bestRep.repRate), k: "best +repair · " + bestRep.label },
      { v: money(cheapest.costPerSolved), k: "cheapest per solved · " + cheapest.label }
    ];
    var mount = $("#statstrip");
    mount.innerHTML = "";
    stats.forEach(function (s) {
      var d = el("div", "stat");
      d.appendChild(el("span", "v", s.v));
      d.appendChild(el("span", "k eyebrow", s.k));
      mount.appendChild(d);
    });

    var run = $("#colophon-run");
    if (run) run.textContent = "run " + lb.run_id + " · generated " + String(lb.generated).slice(0, 10);
  }

  /* ------------------------------------------------------------ 01 · board */

  var COLUMNS = [
    { key: null,        label: "#",              cls: "rank" },
    { key: "label",     label: "Model",          cls: "left", sort: "text" },
    { key: null,        label: "Pass rate  ·  solid = one-shot, hatched = after repair", cls: "left barcell" },
    { key: "oneRate",   label: "One-shot",       sort: "num", dir: -1 },
    { key: "repRate",   label: "+ Repair",       sort: "num", dir: -1 },
    { key: "liftPP",    label: "Lift",           sort: "num", dir: -1 },
    { key: "costPerSolved", label: "$ / solved", sort: "num", dir: 1 },
    { key: null,        label: "",               cls: "chev" }
  ];

  function renderBoard(lb) {
    var rows = lb.models.map(derive);
    var mount = $("#board-mount");
    mount.className = "";
    mount.innerHTML = "";

    var sort = { key: "oneRate", dir: -1 };

    var wrap = el("div", "tablewrap");
    var table = el("table", "board");
    var cap = el("caption");
    cap.textContent = "n = " + lb.dataset.tasks_total + " tasks per model (" +
      Object.keys(lb.dataset.projects).map(function (p) {
        return p + " " + lb.dataset.projects[p];
      }).join(", ") + "). Pass = compiles and every non-baseline test passes.";
    table.appendChild(cap);

    var thead = el("thead");
    var htr = el("tr");
    COLUMNS.forEach(function (c) {
      var th = el("th", c.cls || "", c.label);
      th.scope = "col";
      if (c.sort) {
        th.setAttribute("data-sort", c.key);
        th.tabIndex = 0;
        th.setAttribute("role", "button");
        th.addEventListener("click", function () { applySort(c); });
        th.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); applySort(c); }
        });
      }
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    var tbody = el("tbody");
    table.appendChild(tbody);
    wrap.appendChild(table);
    mount.appendChild(wrap);

    function applySort(c) {
      if (sort.key === c.key) sort.dir = -sort.dir;
      else { sort.key = c.key; sort.dir = c.dir || (c.sort === "text" ? 1 : -1); }
      draw();
    }

    function draw() {
      rows.sort(function (a, b) {
        var x = a[sort.key], y = b[sort.key];
        if (typeof x === "string") return sort.dir * x.localeCompare(y);
        if (x === null) x = Infinity;  // unpriced (zero solves) sorts last
        if (y === null) y = Infinity;
        return sort.dir * (x - y);
      });

      Array.prototype.forEach.call(thead.querySelectorAll("th"), function (th) {
        var k = th.getAttribute("data-sort");
        if (!k) return;
        if (k === sort.key) th.setAttribute("aria-sort", sort.dir === -1 ? "descending" : "ascending");
        else th.removeAttribute("aria-sort");
      });

      tbody.innerHTML = "";
      rows.forEach(function (r, i) { tbody.appendChild(boardRow(r, i, lb)); });
    }

    draw();
  }

  function boardRow(r, i, lb) {
    var frag = document.createDocumentFragment();
    var tr = el("tr", "row");
    tr.tabIndex = 0;
    tr.setAttribute("aria-expanded", "false");

    tr.appendChild(el("td", "rank num", String(i + 1).padStart(2, "0")));

    var tdName = el("td", "left");
    var name = el("div", "mname");
    name.appendChild(el("span", "fam " + r.family));
    name.appendChild(el("span", null, r.label));
    name.appendChild(el("span", "id", r.model));
    tdName.appendChild(name);
    tr.appendChild(tdName);

    var tdBar = el("td", "left barcell");
    tdBar.appendChild(passBar(r.oneRate, r.repRate, true));
    var lab = el("div", "lab");
    var l1 = el("span", "hi", pct(r.oneRate) + " → " + pct(r.repRate));
    var l2 = el("span", null, r.one + " → " + r.rep + " / " + r.n);
    lab.appendChild(l1); lab.appendChild(l2);
    tdBar.appendChild(lab);
    tr.appendChild(tdBar);

    tr.appendChild(el("td", null, pct(r.oneRate)));
    tr.appendChild(el("td", null, pct(r.repRate)));
    tr.appendChild(el("td", "lift", "+" + r.liftPP.toFixed(1) + " pp"));

    var tdCost = el("td", "cost", money(r.costPerSolved));
    tr.appendChild(tdCost);

    var tdChev = el("td", "chev", "▸");
    tr.appendChild(tdChev);

    var detail = el("tr", "detail");
    detail.hidden = true;
    var dtd = el("td");
    dtd.colSpan = COLUMNS.length;
    dtd.appendChild(boardDetail(r, lb));
    detail.appendChild(dtd);

    function toggle() {
      var open = detail.hidden;
      detail.hidden = !open;
      tr.classList.toggle("open", open);
      tr.setAttribute("aria-expanded", String(open));
      tdChev.textContent = open ? "▾" : "▸";
    }
    tr.addEventListener("click", toggle);
    tr.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });

    frag.appendChild(tr);
    frag.appendChild(detail);
    return frag;
  }

  function passBar(oneRate, repRate, ticks) {
    var bar = el("div", "bar");
    var s1 = el("i", "seg-one");
    s1.style.width = (oneRate * 100).toFixed(2) + "%";
    var s2 = el("i", "seg-rep");
    s2.style.width = ((repRate - oneRate) * 100).toFixed(2) + "%";
    bar.appendChild(s1);
    bar.appendChild(s2);
    if (ticks) {
      [25, 50, 75].forEach(function (t) {
        var k = el("i", "tick");
        k.style.left = t + "%";
        bar.appendChild(k);
      });
    }
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", pct(oneRate) + " one-shot, " + pct(repRate) + " after repair, of 100% of tasks");
    return bar;
  }

  function boardDetail(r, lb) {
    var inner = el("div", "inner");

    /* per-project */
    var b1 = el("div", "detail-block");
    b1.appendChild(el("h4", null, "By project"));
    Object.keys(r.raw.by_project).forEach(function (p) {
      var d = r.raw.by_project[p];
      var row = el("div", "projrow");
      row.appendChild(el("span", "pname", p));
      row.appendChild(passBar(d.passed_oneshot / d.n, d.passed_after_repair / d.n, false));
      row.appendChild(el("span", "num", pct(d.passed_after_repair / d.n, 0) + " · " + d.passed_after_repair + "/" + d.n));
      b1.appendChild(row);
    });
    inner.appendChild(b1);

    /* verdict mix, attempt 0 */
    var b2 = el("div", "detail-block");
    b2.appendChild(el("h4", null, "Verdict mix · one-shot"));
    var mix = el("div", "vmix");
    var leg = el("div", "vlegend");
    var v = r.raw.verdicts.attempt_0;
    (lb.verdict_order || Object.keys(v)).forEach(function (k) {
      var count = v[k] || 0;
      if (!count) return;
      var seg = el("i");
      seg.style.width = (count / r.n * 100).toFixed(2) + "%";
      seg.style.background = VERDICT_COLOR[k] || "#444";
      seg.title = k + " · " + count;
      mix.appendChild(seg);
      var s = el("span");
      var sw = el("i");
      sw.style.background = VERDICT_COLOR[k] || "#444";
      s.appendChild(sw);
      s.appendChild(document.createTextNode(k.toLowerCase().replace(/_/g, " ") + " " + count));
      leg.appendChild(s);
    });
    b2.appendChild(mix);
    b2.appendChild(leg);
    inner.appendChild(b2);

    /* budget */
    var b3 = el("div", "detail-block");
    b3.appendChild(el("h4", null, "Budget"));
    var facts = [
      ["attempts", (r.n + (r.n - r.one)) + " (108 one-shot + " + (r.n - r.one) + " repair)"],
      ["tokens in / out", intFmt(r.raw.tokens.in) + " / " + intFmt(r.raw.tokens.out)],
      ["price per Mtok", "$" + r.raw.price_per_mtok_usd.in + " in · $" + r.raw.price_per_mtok_usd.out + " out"],
      ["total spend", money(r.cost)],
      ["per solved task", money(r.costPerSolved)],
      ["median attempt", r.raw.duration_s_median + " s"],
      ["wall clock", (r.raw.duration_s_total / 3600).toFixed(1) + " h"]
    ];
    facts.forEach(function (f) {
      var row = el("div", "projrow");
      row.style.gridTemplateColumns = "1fr auto";
      row.appendChild(el("span", "pname", f[0]));
      row.appendChild(el("span", "num", f[1]));
      b3.appendChild(row);
    });
    inner.appendChild(b3);

    return inner;
  }

  /* --------------------------------------------------------- 02 · taxonomy */

  function renderTaxonomy(tax, lb) {
    var mount = $("#taxonomy-mount");
    mount.className = "";
    mount.innerHTML = "";

    var state = { attempt: "attempt_0", mode: "counts" };
    var order = lb.models.map(function (m) { return m.model; });
    var byModel = {};
    tax.models.forEach(function (m) { byModel[m.model] = m; });
    var models = order.filter(function (k) { return byModel[k]; }).map(function (k) { return byModel[k]; });

    /* controls */
    var controls = el("div", "controls");
    controls.appendChild(segmented("Attempt", [
      { v: "attempt_0", t: "One-shot" },
      { v: "attempt_1", t: "Repair round" }
    ], state.attempt, function (v) { state.attempt = v; draw(); }));
    controls.appendChild(segmented("Scale", [
      { v: "counts", t: "Failure count" },
      { v: "share", t: "Share of failures" }
    ], state.mode, function (v) { state.mode = v; draw(); }));
    mount.appendChild(controls);

    /* legend */
    var legend = el("div", "legend");
    tax.buckets.forEach(function (b) {
      var item = el("div", "item");
      item.title = b.blurb || "";
      var sw = el("i");
      sw.style.background = BUCKET_COLOR[b.id] || "#555";
      item.appendChild(sw);
      item.appendChild(document.createTextNode(b.label));
      legend.appendChild(item);
    });
    mount.appendChild(legend);

    var chart = el("div", "taxchart");
    mount.appendChild(chart);

    var codes = el("div", "codes");
    (tax.top_codes || []).forEach(function (c) {
      var row = el("div", "coderow");
      var code = el("span", "code", c.code);
      code.style.color = BUCKET_COLOR[c.bucket] || "#888";
      row.appendChild(code);
      row.appendChild(el("span", "msg", c.message));
      row.appendChild(el("span", "ct", String(c.count)));
      codes.appendChild(row);
    });
    var codesHead = el("p", "eyebrow");
    codesHead.style.margin = "46px 0 -30px";
    codesHead.textContent = "Most frequent rustc codes across all models · one-shot";
    mount.appendChild(codesHead);
    mount.appendChild(codes);

    function draw() {
      chart.innerHTML = "";
      var totals = models.map(function (m) {
        return Object.keys(m[state.attempt]).reduce(function (s, k) { return s + m[state.attempt][k]; }, 0);
      });
      var scale = state.mode === "share"
        ? { max: 100, ticks: [0, 20, 40, 60, 80, 100] }
        : niceScale(Math.max.apply(null, totals), 6);

      models.forEach(function (m, i) {
        var total = totals[i];
        var row = el("div", "taxrow");
        var lab = el("div", "rowlab");
        lab.appendChild(document.createTextNode(m.label));
        lab.appendChild(el("span", "n", total + " failed"));
        row.appendChild(lab);

        var plot = el("div", "taxplot");
        var grid = el("div", "taxgrid");
        scale.ticks.forEach(function (t) {
          var g = el("i");
          g.style.left = (t / scale.max * 100) + "%";
          grid.appendChild(g);
        });
        plot.appendChild(grid);

        var bar = el("div", "taxbar");
        bar.setAttribute("role", "img");
        var aria = [];
        tax.buckets.forEach(function (b) {
          var val = m[state.attempt][b.id] || 0;
          if (!val) return;
          var widthPct = state.mode === "share"
            ? (val / total * 100) / scale.max * 100
            : (val / scale.max * 100);
          var seg = el("i");
          seg.style.width = widthPct.toFixed(3) + "%";
          seg.style.background = BUCKET_COLOR[b.id] || "#555";
          seg.title = b.label + " · " + val + " (" + (val / total * 100).toFixed(0) + "% of failures)";
          if (widthPct > 7) {
            var t = el("b", null, state.mode === "share" ? Math.round(val / total * 100) + "%" : val);
            t.style.color = inkOn(BUCKET_COLOR[b.id] || "#555555");
            seg.appendChild(t);
          }
          aria.push(b.label + " " + val);
          bar.appendChild(seg);
        });
        bar.setAttribute("aria-label", m.label + ": " + aria.join(", "));
        plot.appendChild(bar);
        row.appendChild(plot);
        chart.appendChild(row);
      });

      var axis = el("div", "taxaxis");
      axis.appendChild(el("div"));
      var ticks = el("div", "ticks");
      scale.ticks.forEach(function (t) {
        var s = el("span", null, state.mode === "share" ? t + "%" : String(t));
        s.style.left = (t / scale.max * 100) + "%";
        ticks.appendChild(s);
      });
      var title = el("span", "title", state.mode === "share"
        ? "share of that model's failed attempts"
        : "failed attempts, of 108 tasks");
      ticks.appendChild(title);
      axis.appendChild(ticks);
      chart.appendChild(axis);
    }

    draw();
  }

  function segmented(label, options, current, onChange) {
    var ctrl = el("div", "ctrl");
    ctrl.appendChild(el("span", "eyebrow", label));
    var group = el("div", "segmented");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", label);
    options.forEach(function (o) {
      var b = el("button", null, o.t);
      b.type = "button";
      b.setAttribute("aria-pressed", String(o.v === current));
      b.addEventListener("click", function () {
        Array.prototype.forEach.call(group.children, function (x) { x.setAttribute("aria-pressed", "false"); });
        b.setAttribute("aria-pressed", "true");
        onChange(o.v);
      });
      group.appendChild(b);
    });
    ctrl.appendChild(group);
    return ctrl;
  }

  /* ------------------------------------------------------------ 03 · repair */

  function renderRepair(lb, tax) {
    var mount = $("#repair-mount");
    mount.className = "";
    mount.innerHTML = "";

    var rows = lb.models.map(derive);
    var failed = rows.reduce(function (s, r) { return s + (r.n - r.one); }, 0);
    var fixed = rows.reduce(function (s, r) { return s + (r.rep - r.one); }, 0);
    var overall = fixed / failed;

    var best = rows.reduce(function (a, b) { return b.recovery > a.recovery ? b : a; });
    var worst = rows.reduce(function (a, b) { return b.recovery < a.recovery ? b : a; });

    var big = el("div", "bignum");
    big.appendChild(el("div", "v num", pct(overall)));
    var p = el("p");
    p.innerHTML =
      "of all failed one-shot attempts are fixed when the model is handed its own compiler error, once. " +
      "That is <strong>" + fixed + " of " + failed + "</strong> failures across " + rows.length +
      " models. The strongest model recovers <strong>" + pct(best.recovery) + "</strong> (" + esc(best.label) +
      "); the weakest recovers <strong>" + pct(worst.recovery) + "</strong> (" + esc(worst.label) +
      "). Self-repair is not a uniform tax discount — it scales with the model.";
    big.appendChild(p);
    mount.appendChild(big);

    /* dumbbells */
    var head = el("p", "eyebrow");
    head.style.margin = "48px 0 0";
    head.textContent = "One-shot → after one repair round · pass rate on 108 tasks";
    mount.appendChild(head);

    var maxRate = Math.max.apply(null, rows.map(function (r) { return r.repRate; }));
    var scale = niceScale(maxRate * 100, 5);
    var db = el("div", "dumbbells");
    rows.forEach(function (r) {
      var row = el("div", "dbrow");
      row.appendChild(el("div", "rowlab", r.label));

      var track = el("div", "dbtrack");
      var grid = el("div", "grid");
      scale.ticks.forEach(function (t) {
        var g = el("i");
        g.style.left = (t / scale.max * 100) + "%";
        grid.appendChild(g);
      });
      track.appendChild(grid);

      var a = r.oneRate * 100 / scale.max * 100;
      var b = r.repRate * 100 / scale.max * 100;
      var link = el("div", "link");
      link.style.left = a + "%";
      link.style.width = Math.max(0, b - a) + "%";
      track.appendChild(link);

      var d1 = el("div", "dot one"); d1.style.left = a + "%";
      d1.title = "one-shot " + pct(r.oneRate);
      var d2 = el("div", "dot rep"); d2.style.left = b + "%";
      d2.title = "after repair " + pct(r.repRate);
      track.appendChild(d1); track.appendChild(d2);
      track.setAttribute("role", "img");
      track.setAttribute("aria-label", r.label + ": " + pct(r.oneRate) + " one-shot, " + pct(r.repRate) + " after repair");
      row.appendChild(track);

      var vals = el("div", "vals");
      vals.appendChild(el("span", "a", pct(r.oneRate)));
      vals.appendChild(el("span", null, "→ " + pct(r.repRate)));
      vals.appendChild(el("span", "d", pct(r.recovery, 0) + " self-fix"));
      row.appendChild(vals);

      db.appendChild(row);
    });

    var axis = el("div", "dbaxis");
    axis.appendChild(el("div"));
    var ticks = el("div", "ticks");
    scale.ticks.forEach(function (t) {
      var s = el("span", null, t + "%");
      s.style.left = (t / scale.max * 100) + "%";
      ticks.appendChild(s);
    });
    axis.appendChild(ticks);
    axis.appendChild(el("div"));
    db.appendChild(axis);
    mount.appendChild(db);

    /* what the repair round actually clears — bucket share shift */
    var t0 = {}, t1 = {}, s0 = 0, s1 = 0;
    tax.buckets.forEach(function (b) { t0[b.id] = 0; t1[b.id] = 0; });
    tax.models.forEach(function (m) {
      tax.buckets.forEach(function (b) {
        t0[b.id] += m.attempt_0[b.id] || 0;
        t1[b.id] += m.attempt_1[b.id] || 0;
      });
    });
    tax.buckets.forEach(function (b) { s0 += t0[b.id]; s1 += t1[b.id]; });

    var deltas = tax.buckets.map(function (b) {
      return { b: b, before: t0[b.id] / s0 * 100, after: t1[b.id] / s1 * 100 };
    }).map(function (d) { d.delta = d.after - d.before; return d; });
    var maxAbs = Math.max.apply(null, deltas.map(function (d) { return Math.abs(d.delta); }));

    var h2 = el("p", "eyebrow");
    h2.style.margin = "56px 0 0";
    h2.textContent = "What survives the repair round · change in each bucket's share of remaining failures";
    mount.appendChild(h2);

    var note = el("p");
    note.style.cssText = "margin:14px 0 0;max-width:70ch;color:var(--ink-2);font-size:14.5px;line-height:1.6";
    note.textContent =
      "Bars pointing left are cleared disproportionately by the repair round; bars pointing right are what is " +
      "left over. Missing imports and type mismatches are bookkeeping — one look at the error is usually enough. " +
      "Borrow-checker errors and wrong semantics are not, and they grow as a share of everything still failing.";
    mount.appendChild(note);

    var shifts = el("div", "shifts");
    deltas.sort(function (a, b) { return a.delta - b.delta; });
    deltas.forEach(function (d) {
      var row = el("div", "shiftrow");
      row.appendChild(el("div", "rowlab", d.b.short || d.b.label));
      var track = el("div", "shifttrack");
      var zero = el("div", "zero"); zero.style.left = "50%";
      track.appendChild(zero);
      var fill = el("div", "fill");
      var half = Math.abs(d.delta) / maxAbs * 50;
      if (d.delta < 0) { fill.style.right = "50%"; fill.style.width = half + "%"; fill.style.background = "var(--pass)"; }
      else { fill.style.left = "50%"; fill.style.width = half + "%"; fill.style.background = BUCKET_COLOR[d.b.id]; }
      track.appendChild(fill);
      track.title = d.b.label + ": " + d.before.toFixed(1) + "% → " + d.after.toFixed(1) + "% of failures";
      row.appendChild(track);
      var del = el("div", "delta " + (d.delta < 0 ? "down" : "up"),
        (d.delta > 0 ? "+" : "") + d.delta.toFixed(1) + " pp");
      row.appendChild(del);
      shifts.appendChild(row);
    });
    mount.appendChild(shifts);
  }

  /* ----------------------------------------------------------- 04 · gallery */

  function renderGallery(gal, tax) {
    var mount = $("#gallery-mount");
    mount.className = "";
    mount.innerHTML = "";

    var bucketLabel = {};
    tax.buckets.forEach(function (b) { bucketLabel[b.id] = b.short || b.label; });

    var present = [];
    gal.entries.forEach(function (e) { if (present.indexOf(e.bucket) === -1) present.push(e.bucket); });

    var filters = el("div", "filters");
    var current = "all";
    var cards = el("div", "cards");

    function mkFilter(v, t, count) {
      var b = el("button", null, t + "  " + count);
      b.type = "button";
      b.setAttribute("aria-pressed", String(v === current));
      b.addEventListener("click", function () {
        current = v;
        Array.prototype.forEach.call(filters.children, function (x) { x.setAttribute("aria-pressed", "false"); });
        b.setAttribute("aria-pressed", "true");
        drawCards();
      });
      return b;
    }
    filters.appendChild(mkFilter("all", "All failures", gal.entries.length));
    present.forEach(function (id) {
      var n = gal.entries.filter(function (e) { return e.bucket === id; }).length;
      filters.appendChild(mkFilter(id, bucketLabel[id] || id, n));
    });
    mount.appendChild(filters);
    mount.appendChild(cards);

    function drawCards() {
      cards.innerHTML = "";
      gal.entries
        .filter(function (e) { return current === "all" || e.bucket === current; })
        .forEach(function (e) { cards.appendChild(galleryCard(e, bucketLabel)); });
    }
    drawCards();
  }

  function galleryCard(e, bucketLabel) {
    var card = el("article", "card");

    var header = el("header");
    var chips = el("div", "chips");
    chips.appendChild(el("span", "chip model", e.model_label || e.model));
    chips.appendChild(el("span", "chip verdict", e.verdict.replace(/_/g, " ")));
    var bchip = el("span", "chip bucket", bucketLabel[e.bucket] || e.bucket);
    bchip.style.color = BUCKET_COLOR[e.bucket] || "var(--ink-2)";
    bchip.style.borderColor = (BUCKET_COLOR[e.bucket] || "#555") + "66";
    chips.appendChild(bchip);
    chips.appendChild(el("span", "chip attempt", "attempt " + e.attempt + (e.attempt === 1 ? " · after repair" : " · one-shot")));
    if (e.error_class_hint && e.error_class_hint.length) {
      chips.appendChild(el("span", "chip", e.error_class_hint.join(" ")));
    }
    header.appendChild(chips);
    header.appendChild(el("h4", null, e.title));
    if (e.takeaway) header.appendChild(el("p", "takeaway", e.takeaway));
    var meta = e.project + " · " + e.task_id;
    if (e.failing_tests && e.failing_tests.length) meta += " · failing: " + e.failing_tests.join(", ");
    header.appendChild(el("p", "taskid", meta));
    card.appendChild(header);

    var panes = el("div", "panes");

    var p1 = el("div", "pane");
    var h1 = el("div", "phead");
    h1.appendChild(el("span", "eyebrow", "Model output"));
    h1.appendChild(el("span", "eyebrow", e.model));
    p1.appendChild(h1);
    var pre1 = el("pre");
    var code1 = document.createElement("code");
    code1.innerHTML = window.PB_HL.rust(e.model_code);
    pre1.appendChild(code1);
    p1.appendChild(pre1);
    panes.appendChild(p1);

    var p2 = el("div", "pane");
    var h2 = el("div", "phead");
    h2.appendChild(el("span", "eyebrow", e.verdict === "TEST_FAIL" ? "cargo test" : "rustc"));
    h2.appendChild(el("span", "eyebrow", e.project));
    p2.appendChild(h2);
    var pre2 = el("pre");
    var code2 = document.createElement("code");
    code2.innerHTML = window.PB_HL.rustc(e.compiler_error);
    pre2.appendChild(code2);
    p2.appendChild(pre2);
    panes.appendChild(p2);

    card.appendChild(panes);
    return card;
  }

  /* ------------------------------------------------------------- 05 · trend */

  var SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }

  function renderTrend(tr) {
    var mount = $("#trend-mount");
    mount.className = "";
    mount.innerHTML = "";

    var box = el("div", "chartbox");
    mount.appendChild(box);

    var notes = el("div", "trendnotes");
    tr.points.forEach(function (p) {
      var n = el("div", "n");
      n.appendChild(el("div", "d", p.date + "  ·  " + p.models_run + " models"));
      var body = el("p");
      body.innerHTML = "<strong style=\"color:var(--ink)\">" + esc(p.best_model_label) + "</strong> led at " +
        esc(pct(p.best_passed_oneshot / tr.tasks_total)) + " one-shot. " + esc(p.note || "");
      n.appendChild(body);
      notes.appendChild(n);
    });
    mount.appendChild(notes);

    var SERIES = [
      { key: "best_passed_after_repair", label: "Frontier, after repair", color: "#f2664c", dash: "5 4" },
      { key: "best_passed_oneshot", label: "Frontier, one-shot", color: "#ce422b", dash: null },
      { key: "median_passed_oneshot", label: "Field median, one-shot", color: "#7d848f", dash: null }
    ];

    function draw() {
      var w = Math.max(300, box.clientWidth - 40);
      var narrow = w < 480;
      var h = narrow ? 250 : 320;
      var pad = { l: narrow ? 40 : 52, r: 12, t: 30, b: narrow ? 52 : 58 };
      var iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;

      var xs = tr.points.map(function (p) { return new Date(p.date + "T00:00:00Z").getTime(); });
      var x0 = xs[0], x1 = xs[xs.length - 1];
      var spanX = (x1 - x0) || 1;
      var maxRate = Math.max.apply(null, tr.points.map(function (p) {
        return p.best_passed_after_repair / tr.tasks_total;
      }));
      var scale = niceScale(maxRate * 100, 5);

      var X = function (t) { return pad.l + (t - x0) / spanX * iw; };
      var Y = function (rate) { return pad.t + ih - (rate * 100 / scale.max) * ih; };

      box.innerHTML = "";
      var svg = svgEl("svg", {
        viewBox: "0 0 " + w + " " + h,
        width: w, height: h,
        role: "img",
        "aria-label": "Best and median pass rate per sweep, " + tr.points[0].date + " to " + tr.points[tr.points.length - 1].date
      });

      /* y gridlines + labels */
      scale.ticks.forEach(function (t) {
        var y = Y(t / 100);
        svg.appendChild(svgEl("line", {
          x1: pad.l, x2: pad.l + iw, y1: y, y2: y,
          stroke: t === 0 ? "rgba(255,255,255,.22)" : "rgba(255,255,255,.06)", "stroke-width": 1
        }));
        var lab = svgEl("text", {
          x: pad.l - 10, y: y + 4, "text-anchor": "end",
          fill: "#74716c", "font-size": narrow ? 10 : 11,
          "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace"
        });
        lab.textContent = t + "%";
        svg.appendChild(lab);
      });

      /* y axis title */
      var yt = svgEl("text", {
        x: 0, y: 10, "text-anchor": "start",
        fill: "#74716c", "font-size": 10, "letter-spacing": ".14em",
        "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace"
      });
      yt.textContent = "PASS RATE, % OF 108 TASKS";
      svg.appendChild(yt);

      /* series */
      SERIES.forEach(function (s) {
        var d = tr.points.map(function (p, i) {
          return (i ? "L" : "M") + X(xs[i]).toFixed(1) + " " + Y(p[s.key] / tr.tasks_total).toFixed(1);
        }).join(" ");
        var path = svgEl("path", {
          d: d, fill: "none", stroke: s.color, "stroke-width": 2,
          "stroke-linecap": "round", "stroke-linejoin": "round"
        });
        if (s.dash) path.setAttribute("stroke-dasharray", s.dash);
        svg.appendChild(path);

        tr.points.forEach(function (p, i) {
          svg.appendChild(svgEl("circle", {
            cx: X(xs[i]), cy: Y(p[s.key] / tr.tasks_total), r: 3.6,
            fill: "#08090b", stroke: s.color, "stroke-width": 2
          }));
        });
      });

      /* value labels on the one-shot frontier line */
      tr.points.forEach(function (p, i) {
        var v = p.best_passed_oneshot / tr.tasks_total;
        var t = svgEl("text", {
          x: X(xs[i]) + (i === tr.points.length - 1 ? -7 : 7),
          y: Y(v) - 11,
          "text-anchor": i === tr.points.length - 1 ? "end" : "start",
          fill: "#ece9e5", "font-size": narrow ? 11 : 12.5,
          "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace"
        });
        t.textContent = pct(v);
        svg.appendChild(t);
      });

      /* x axis */
      tr.points.forEach(function (p, i) {
        var x = X(xs[i]);
        svg.appendChild(svgEl("line", {
          x1: x, x2: x, y1: pad.t + ih, y2: pad.t + ih + 5,
          stroke: "rgba(255,255,255,.22)", "stroke-width": 1
        }));
        var anchor = i === 0 ? "start" : (i === tr.points.length - 1 ? "end" : "middle");
        var t1 = svgEl("text", {
          x: x, y: pad.t + ih + 20, "text-anchor": anchor,
          fill: "#a8a49e", "font-size": narrow ? 10 : 11.5,
          "font-family": "ui-monospace, SFMono-Regular, Menlo, monospace"
        });
        t1.textContent = p.date;
        svg.appendChild(t1);
        var t2 = svgEl("text", {
          x: x, y: pad.t + ih + 36, "text-anchor": anchor,
          fill: "#74716c", "font-size": narrow ? 9.5 : 11,
          "font-family": "-apple-system, system-ui, sans-serif"
        });
        t2.textContent = p.best_model_label;
        svg.appendChild(t2);
      });

      box.appendChild(svg);

      var legend = el("div", "legend");
      legend.style.margin = "18px 0 4px";
      SERIES.forEach(function (s) {
        var item = el("div", "item");
        var sw = el("i");
        sw.style.background = s.color;
        if (s.dash) sw.style.opacity = ".75";
        item.appendChild(sw);
        item.appendChild(document.createTextNode(s.label));
        legend.appendChild(item);
      });
      box.appendChild(legend);
    }

    draw();

    var timer = null;
    window.addEventListener("resize", function () {
      clearTimeout(timer);
      timer = setTimeout(draw, 140);
    });
  }

  /* --------------------------------------------------------------- nav spy */

  function navSpy() {
    var links = Array.prototype.slice.call(document.querySelectorAll(".secnav a[href^='#']"));
    var map = {};
    links.forEach(function (a) {
      var t = document.querySelector(a.getAttribute("href"));
      if (t) map[t.id] = a;
    });
    if (!("IntersectionObserver" in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          links.forEach(function (a) { a.classList.remove("active"); });
          if (map[en.target.id]) map[en.target.id].classList.add("active");
        }
      });
    }, { rootMargin: "-45% 0px -50% 0px", threshold: 0 });
    Object.keys(map).forEach(function (id) { io.observe(document.getElementById(id)); });
  }

  /* ------------------------------------------------------------------ boot */

  load().then(function (data) {
    DATA = data;
    renderSampleBanner(data);
    renderStats(data.leaderboard);
    renderBoard(data.leaderboard);
    renderTaxonomy(data.taxonomy, data.leaderboard);
    renderRepair(data.leaderboard, data.taxonomy);
    renderGallery(data.gallery, data.taxonomy);
    renderTrend(data.trend);
    navSpy();
  }).catch(function (err) {
    if (window.console) console.error("[portbench] data load failed:", err);
    failAll(err);
  });
})();
