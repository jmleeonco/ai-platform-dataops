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
    if (!document.getElementById("tab-" + tab)) {
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
