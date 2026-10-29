// Run page: three tabs (extracted values, DB tables, DICOM metadata) and, inside the DB tab, one table at a time.
// The location hash keeps the choice across reloads: #source, #db, #db-<table>, #dicom.
(function () {
  var tabs = document.querySelectorAll("nav.tabs a[data-tab]");
  if (!tabs.length) return;
  var subs = document.querySelectorAll("#tab-db .sub");

  function showSub(name) {
    Array.prototype.forEach.call(subs, function (s) { s.hidden = s.id !== "sub-" + name; });
    Array.prototype.forEach.call(document.querySelectorAll("a[data-sub]"), function (a) {
      a.classList.toggle("on", a.dataset.sub === name);
    });
  }
  function show(hash) {
    var h = (hash || "").replace(/^#/, ""), tab = h.split("-")[0], sub = h.indexOf("db-") === 0 ? h.slice(3) : null;
    var box = document.getElementById("tab-" + tab);
    if (box && h && box.closest("details")) box.closest("details").open = true;   // a tab named in the URL
    if (!box) {
      var more = h && document.getElementById(h);        // e.g. #dataset after a form post
      if (more && more.closest("details")) more.closest("details").open = true;
      tab = "source";
    }
    Array.prototype.forEach.call(tabs, function (a) { a.classList.toggle("on", a.dataset.tab === tab); });
    Array.prototype.forEach.call(document.querySelectorAll("section.tab"), function (s) {
      s.hidden = s.id !== "tab-" + tab;
    });
    if (subs.length) showSub(sub && document.getElementById("sub-" + sub) ? sub : subs[0].id.slice(4));
  }
  function go(hash) {
    history.replaceState(null, "", hash);
    show(hash);
  }
  Array.prototype.forEach.call(tabs, function (a) {
    a.addEventListener("click", function (e) { e.preventDefault(); go("#" + a.dataset.tab); });
  });
  Array.prototype.forEach.call(document.querySelectorAll("a[data-sub]"), function (a) {
    a.addEventListener("click", function (e) { e.preventDefault(); go("#db-" + a.dataset.sub); });
  });
  show(location.hash);
})();

// Sample view: one sample at a time (#case-N), inside it one DICOM series at a time; the checkbox shows the
// empty / fixed-value rows too.
(function () {
  var cases = document.querySelectorAll("a[data-case]");
  if (!cases.length) return;
  function each(sel, fn) { Array.prototype.forEach.call(document.querySelectorAll(sel), fn); }
  function showSer(id) {
    var box = document.getElementById(id).closest(".case");
    Array.prototype.forEach.call(box.querySelectorAll(".ser"), function (d) { d.hidden = d.id !== id; });
    Array.prototype.forEach.call(box.querySelectorAll("a[data-ser]"), function (a) { a.classList.toggle("on", a.dataset.ser === id); });
  }
  function showCase(id) {
    if (!document.getElementById(id) || !document.getElementById(id).classList.contains("case")) id = cases[0].dataset.case;
    each(".case", function (d) { d.hidden = d.id !== id; });
    each("a[data-case]", function (a) { a.classList.toggle("on", a.dataset.case === id); });
  }
  each("a[data-case]", function (a) {
    a.addEventListener("click", function (e) { e.preventDefault(); history.replaceState(null, "", "#" + a.dataset.case); showCase(a.dataset.case); });
  });
  each("a[data-ser]", function (a) {
    a.addEventListener("click", function (e) { e.preventDefault(); showSer(a.dataset.ser); });
  });
  each(".case", function (d) { var first = d.querySelector("a[data-ser]"); if (first) showSer(first.dataset.ser); });
  var all = document.getElementById("cmp-all"), wrap = document.getElementById("cmp-wrap");
  if (all && wrap) all.addEventListener("change", function () { wrap.classList.toggle("all", all.checked); });
  showCase(location.hash.replace(/^#/, ""));
})();
