// Metadata page: the full per-series table (fetched as JSON), with search, filters, column groups,
// sorting and paging. Values are dataset text: always set with textContent, never as HTML.
(function () {
  var box = document.getElementById("mt");
  if (!box) return;
  var PAGE = 100, data = null, view = [], page = 1, sortKey = null, sortDir = 1;
  var $ = function (id) { return document.getElementById(id); };
  var table = $("mt-table"), info = $("mt-info"), pager = $("mt-pager");

  function text(v) {
    if (v === null || v === undefined) return "";
    if (v === true) return "Y";
    if (v === false) return "N";
    if (Array.isArray(v)) return v.map(text).join(" ");
    return String(v);
  }
  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined) e.textContent = txt;
    return e;
  }
  function groupsOn() {
    var on = {};
    document.querySelectorAll("#mt-groups input").forEach(function (c) { on[c.value] = c.checked; });
    return on;
  }
  function cols() {
    var on = groupsOn();
    return data.columns.filter(function (c) { return on[c.group]; });
  }
  function filter() {
    var q = $("mt-q").value.trim().toLowerCase(), sp = $("mt-split").value, kd = $("mt-kind").value,
        inc = $("mt-inc").value;
    view = data.rows.filter(function (r) {
      if (sp && r.split !== sp) return false;
      if (kd && r.kind !== kd) return false;
      if (inc === "in" && !r.included) return false;
      if (inc === "out" && r.included) return false;
      if (inc === "pilot" && !r.pilot) return false;
      if (q) {
        for (var k in r) if (text(r[k]).toLowerCase().indexOf(q) >= 0) return true;
        return false;
      }
      return true;
    });
    if (sortKey) {
      view.sort(function (a, b) {
        var x = a[sortKey], y = b[sortKey];
        if (typeof x === "number" && typeof y === "number") return (x - y) * sortDir;
        return text(x).localeCompare(text(y), "ko", { numeric: true }) * sortDir;
      });
    }
    page = 1;
    render();
  }
  function render() {
    var cs = cols();
    table.textContent = "";
    var g = el("tr"), h = el("tr");
    var last = null, span = null;
    cs.forEach(function (c) {
      if (c.group !== last) {
        span = el("th", "grp g-" + c.group, c.group_label);
        span.colSpan = 0;
        g.appendChild(span);
        last = c.group;
      }
      span.colSpan += 1;
      var th = el("th", "sortable");
      th.appendChild(el("span", "", c.label + (sortKey === c.key ? (sortDir > 0 ? " ▲" : " ▼") : "")));
      var tip = [];
      if (c.dicom || c.db) {
        var d = el("div", "dests");
        if (c.dicom) { d.appendChild(el("span", "chip c-dicom", "D")); tip.push("DICOM: " + c.dicom); }
        if (c.db) { d.appendChild(el("span", "chip c-db", "DB")); tip.push("DB: " + c.db); }
        th.appendChild(d);
      } else {
        tip.push("원본에만 남음");
      }
      if (c.note) tip.push(c.note);
      th.title = tip.join("\n");
      th.addEventListener("click", function () {
        sortDir = sortKey === c.key ? -sortDir : 1;
        sortKey = c.key;
        filter();
      });
      h.appendChild(th);
    });
    table.appendChild(g);
    table.appendChild(h);
    view.slice((page - 1) * PAGE, page * PAGE).forEach(function (r) {
      var tr = el("tr", r.included ? "" : "muted");
      cs.forEach(function (c) {
        var v = r[c.key], td = el("td", typeof v === "number" ? "num" : "small");
        if (c.key === "series_uid" && r.included && r.kind !== "rtstruct") {
          var a = el("a", "mono", text(v));
          a.href = "/runs/" + encodeURIComponent(box.dataset.run) + "/series/" + encodeURIComponent(v);
          td.appendChild(a);
        } else {
          td.textContent = text(v);
        }
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    info.textContent = view.length + " / " + data.rows.length + " 시리즈 · 열 " + cs.length + "개";
    pager.textContent = "";
    var pages = Math.max(1, Math.ceil(view.length / PAGE));
    if (pages > 1) {
      for (var i = 1; i <= pages; i++) {
        (function (i) {
          var a = el(i === page ? "span" : "a", "", String(i));
          if (i !== page) {
            a.href = "#mt";
            a.addEventListener("click", function (e) { e.preventDefault(); page = i; render(); });
          }
          pager.appendChild(a);
        })(i);
      }
    }
  }

  $("mt-controls").addEventListener("submit", function (e) { e.preventDefault(); });
  ["mt-split", "mt-kind", "mt-inc"].forEach(function (id) { $(id).addEventListener("change", filter); });
  var t = null;
  $("mt-q").addEventListener("input", function () { clearTimeout(t); t = setTimeout(filter, 200); });
  document.querySelectorAll("#mt-groups input").forEach(function (c) { c.addEventListener("change", render); });

  fetch(box.dataset.src, { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (d) { data = d; filter(); })
    .catch(function (e) { info.textContent = "표를 불러오지 못했습니다: " + e.message; });
})();
