/* PortBench — tiny syntax highlighter.
   Two functions, no dependencies, no CDN: PB_HL.rust() for model output and
   PB_HL.rustc() for compiler / test output. Deliberately small — this only ever
   has to look right on short Rust functions and rustc diagnostics, not to be a
   general-purpose lexer. Everything is escaped before it reaches innerHTML. */

(function (global) {
  "use strict";

  function esc(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;";
    });
  }

  var KEYWORDS =
    "as|async|await|break|const|continue|crate|dyn|else|enum|extern|false|fn|for|if|impl|" +
    "in|let|loop|match|mod|move|mut|pub|ref|return|self|Self|static|struct|super|trait|true|" +
    "type|union|unsafe|use|where|while";

  /* Single ordered alternation; first matching group wins. Order matters:
     comments and string literals must be consumed before anything can look
     inside them, and char literals must beat lifetimes ('a' vs 'a). */
  var RUST_RE = new RegExp([
    "(\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/)",                            /* 1 comment   */
    "(\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\\\n]|\\\\.)')",                /* 2 literal   */
    "(#!?\\[[^\\]]*\\])",                                                /* 3 attribute */
    "('[A-Za-z_][A-Za-z0-9_]*)",                                         /* 4 lifetime  */
    "\\b(" + KEYWORDS + ")\\b",                                          /* 5 keyword   */
    "\\b(\\d[\\d_]*(?:\\.\\d+)?(?:[iuf](?:8|16|32|64|128|size))?)\\b",   /* 6 number    */
    "\\b([a-z_][A-Za-z0-9_]*!)",                                         /* 7 macro     */
    "\\b([a-z_][A-Za-z0-9_]*)(?=\\s*\\()",                               /* 8 call      */
    "\\b([A-Z][A-Za-z0-9_]*)\\b"                                         /* 9 type      */
  ].join("|"), "g");

  var CLASSES = [null, "t-cmt", "t-str", "t-attr", "t-life", "t-kw", "t-num", "t-mac", "t-fn", "t-typ"];

  function rust(code) {
    var out = "", last = 0, m, g, cls;
    RUST_RE.lastIndex = 0;
    while ((m = RUST_RE.exec(code)) !== null) {
      if (m[0].length === 0) { RUST_RE.lastIndex++; continue; }
      if (m.index > last) out += esc(code.slice(last, m.index));
      cls = null;
      for (g = 1; g < CLASSES.length; g++) {
        if (m[g] !== undefined) { cls = CLASSES[g]; break; }
      }
      out += cls ? '<span class="' + cls + '">' + esc(m[0]) + "</span>" : esc(m[0]);
      last = m.index + m[0].length;
    }
    return out + esc(code.slice(last));
  }

  function span(cls, text) { return '<span class="' + cls + '">' + esc(text) + "</span>"; }

  function rustcLine(line) {
    var m;

    /* error[E0502]: message  |  error: message  |  warning: message */
    m = /^(error|warning)(\[[A-Z]\d{4}\])?: (.*)$/.exec(line);
    if (m) {
      return span("e-err", m[1]) + (m[2] ? span("e-code", m[2]) : "") +
        span("e-err", ":") + " " + span("e-msg", m[3]);
    }

    /*   --> path/to/file.rs:214:5 */
    m = /^(\s*)-->(\s*)(\S+)$/.exec(line);
    if (m) return esc(m[1]) + span("e-gut", "-->") + esc(m[2]) + span("e-loc", m[3]);

    /* gutter lines: "214 |     &key"  and  "    |     ^^^^ label" */
    m = /^(\s*\d*\s*\|)(.*)$/.exec(line);
    if (m) {
      var gutter = span("e-gut", m[1]), rest = m[2];
      if (/^\s*[\^\-~+]/.test(rest)) return gutter + span("e-caret", rest);
      if (rest.trim() === "") return gutter;
      return gutter + rust(rest);
    }

    /*   = note: ... / = help: ... */
    m = /^(\s*)=\s*(note|help|warning)(: )(.*)$/.exec(line);
    if (m) {
      return esc(m[1]) + span(m[2] === "help" ? "e-help" : "e-note", "= " + m[2] + ":") +
        " " + span("e-plain", m[4]);
    }

    /* left-margin help:/note: blocks */
    m = /^(help|note)(: )(.*)$/.exec(line);
    if (m) return span(m[1] === "help" ? "e-help" : "e-note", m[1] + ":") + " " + span("e-plain", m[3]);

    /* libtest output */
    if (/^test result: FAILED/.test(line)) return span("e-err", line);
    if (/^test result: ok/.test(line)) return span("e-help", line);
    if (/^failures:/.test(line)) return span("e-err", line);
    if (/^----.*----$/.test(line)) return span("e-gut", line);
    m = /^(thread )('[^']*')( panicked at )(.*)$/.exec(line);
    if (m) {
      return span("e-plain", m[1]) + span("e-msg", m[2]) + span("e-err", m[3]) + span("e-loc", m[4]);
    }
    if (/^assertion .* failed/.test(line)) return span("e-err", line);

    return span("e-plain", line);
  }

  function rustc(text) {
    return String(text).split("\n").map(rustcLine).join("\n");
  }

  global.PB_HL = { rust: rust, rustc: rustc, escape: esc };
})(window);
