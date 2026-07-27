/* NeoCloud consoles — runtime KO/EN language toggle.
 *
 * Design: instead of externalising every string into t('key') call sites
 * (a multi-thousand-line refactor of vanilla-JS template renderers), this
 * engine translates the *rendered DOM* on the fly. One KO→EN dictionary
 * therefore covers BOTH console-native strings AND backend-surfaced Korean
 * (NOCP/emulator `detail`/`event` text that reaches the DOM as-is) — the
 * backend needs no change for the toggle.
 *
 * - `NC_I18N.dict`      exact-match  { "한국어": "English" }
 * - `NC_I18N.patterns`  interpolated [ [/… (\d+) …/, "… $1 …"] ]  (for strings
 *                       with numbers/ids the exact map can't capture)
 * - text nodes + [title]/[placeholder]/[aria-label] attrs are translated;
 *   each node's original KO is cached (WeakMap) so KO restore is lossless.
 * - a debounced MutationObserver re-applies after every re-render, so no
 *   render function has to be touched.
 * - toggle KO|EN in `.tb-right`, persisted in localStorage['nc-lang'],
 *   synced across consoles/tabs via the `storage` event. Default: ko.
 *
 * The dictionary lives in shared/i18n-dict.js (loaded before this file).
 */
(function () {
  var KEY = "nc-lang";
  var W = window;
  var DICT = (W.NC_I18N && W.NC_I18N.dict) || {};
  var PAT = (W.NC_I18N && W.NC_I18N.patterns) || [];
  // precompile patterns → [{re, repl}]
  var PATS = PAT.map(function (p) { return { re: p[0], repl: p[1] }; });

  var origText = new WeakMap();   // textNode → original KO
  var origAttr = new WeakMap();   // element → { attr: originalKO }
  var ATTRS = ["title", "placeholder", "aria-label"];
  var hasKo = /[가-힣]/;

  function lang() {
    var v = null;
    try { v = localStorage.getItem(KEY); } catch (e) {}
    return v === "en" ? "en" : "ko";
  }

  function translate(ko) {
    var s = ko.trim();
    if (!s) return null;
    if (DICT.hasOwnProperty(s)) {
      // preserve surrounding whitespace of the original node
      return ko.replace(s, DICT[s]);
    }
    for (var i = 0; i < PATS.length; i++) {
      if (PATS[i].re.test(s)) {
        return ko.replace(s, s.replace(PATS[i].re, PATS[i].repl));
      }
    }
    return null; // untranslated — leave KO (surfaces as coverage gap)
  }

  function applyTextNode(node, en) {
    if (en) {
      var cur = node.nodeValue;
      if (!hasKo.test(cur)) return;
      if (!origText.has(node)) origText.set(node, cur);
      var t = translate(origText.get(node));
      if (t !== null && t !== node.nodeValue) node.nodeValue = t;
    } else if (origText.has(node)) {
      node.nodeValue = origText.get(node);
    }
  }

  function applyAttrs(el, en) {
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      if (!el.hasAttribute(a)) continue;
      if (en) {
        var cur = el.getAttribute(a);
        if (!hasKo.test(cur)) continue;
        var cache = origAttr.get(el) || {};
        if (!(a in cache)) { cache[a] = cur; origAttr.set(el, cache); }
        var t = translate(cache[a]);
        if (t !== null && t !== el.getAttribute(a)) el.setAttribute(a, t);
      } else {
        var c = origAttr.get(el);
        if (c && a in c) el.setAttribute(a, c[a]);
      }
    }
  }

  function walk(root, en) {
    if (!root) return;
    // text nodes
    var tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n, batch = [];
    while ((n = tw.nextNode())) batch.push(n);
    batch.forEach(function (t) {
      var p = t.parentNode;
      if (p && (p.nodeName === "SCRIPT" || p.nodeName === "STYLE")) return;
      applyTextNode(t, en);
    });
    // attrs
    var els = root.querySelectorAll ? root.querySelectorAll("[title],[placeholder],[aria-label]") : [];
    for (var i = 0; i < els.length; i++) applyAttrs(els[i], en);
    if (root.nodeType === 1) applyAttrs(root, en);
  }

  var _t;
  function reapply() {
    clearTimeout(_t);
    _t = setTimeout(function () { walk(document.body, lang() === "en"); }, 30);
  }

  // translate freshly-added subtrees synchronously so live re-renders
  // (steppers, telemetry ticks) never flash untranslated Korean.
  function onMutations(muts) {
    if (lang() !== "en") { reapply(); return; }
    for (var i = 0; i < muts.length; i++) {
      var m = muts[i];
      if (m.type === "childList") {
        for (var j = 0; j < m.addedNodes.length; j++) {
          var nd = m.addedNodes[j];
          if (nd.nodeType === 3) applyTextNode(nd, true);
          else if (nd.nodeType === 1) walk(nd, true);
        }
      } else if (m.type === "characterData") {
        origText.delete(m.target);   // content changed → re-cache new original
        applyTextNode(m.target, true);
      }
    }
  }

  function setLang(l) {
    try { localStorage.setItem(KEY, l); } catch (e) {}
    document.documentElement.lang = l;
    syncToggle(l);
    walk(document.body, l === "en");
  }

  function syncToggle(l) {
    var t = document.getElementById("lang-toggle");
    if (!t) return;
    var ko=t.querySelector('[data-lang="ko"]'), en=t.querySelector('[data-lang="en"]');
    ko.style.background = l==="ko"?"#3d8ad0":"transparent"; ko.style.color=l==="ko"?"#fff":"#8aa0b4"; ko.style.fontWeight=l==="ko"?"700":"400";
    en.style.background = l==="en"?"#3d8ad0":"transparent"; en.style.color=l==="en"?"#fff":"#8aa0b4"; en.style.fontWeight=l==="en"?"700":"400";
  }

  function mount() {
    if (document.getElementById("lang-toggle")) return;
    var host = document.querySelector(".tb-right") || document.querySelector(".topbar") || document.querySelector("header .right") || document.querySelector("header");
    if (!host) return;
    var t = document.createElement("span");
    t.id = "lang-toggle";
    t.className = "lang-toggle";
    t.style.cssText = "display:inline-flex;border:1px solid #2a3a4f;font-family:Menlo,monospace;margin-left:10px;vertical-align:middle";
    t.innerHTML = '<span data-lang="ko" title="한국어" style="padding:3px 8px;font-size:10px;cursor:pointer;color:#8aa0b4;user-select:none">KO</span>' +
                  '<span data-lang="en" title="English" style="padding:3px 8px;font-size:10px;cursor:pointer;color:#8aa0b4;user-select:none;border-left:1px solid #2a3a4f">EN</span>';
    t.addEventListener("click", function (e) {
      var l = e.target && e.target.getAttribute("data-lang");
      if (l) setLang(l);
    });
    host.appendChild(t);
    syncToggle(lang());
  }

  // expose for renderers that want to translate a string directly
  W.NC_I18N = W.NC_I18N || {};
  W.NC_I18N.t = function (ko) {
    if (lang() !== "en") return ko;
    var t = translate(ko);
    return t === null ? ko : t;
  };
  W.NC_I18N.reapply = reapply;
  W.NC_I18N.lang = lang;

  function init() {
    mount();
    document.documentElement.lang = lang();
    walk(document.body, lang() === "en");
    // re-translate whatever the SPA renders afterwards
    var target = document.body;   // body-wide: catches sidebar/topbar re-renders too
    try {
      new MutationObserver(onMutations).observe(target,
        { childList: true, subtree: true, characterData: true });
    } catch (e) {}
    // also on route/modal changes if the bus exists
    if (W.NC && NC.bus && NC.bus.on) {
      NC.bus.on("route", reapply); NC.bus.on("modal.open", reapply);
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }

  addEventListener("storage", function (e) {
    if (e.key === KEY) { syncToggle(lang()); walk(document.body, lang() === "en"); }
  });
})();
