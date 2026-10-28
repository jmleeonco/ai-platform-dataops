// Report section 6: the series table in the fixed schema (fetched as JSON) with search, filters, sorting
// and paging. Values are dataset text: always set with textContent, never as HTML.
(function () {
  var box = document.getElementById("mt");
  if (!box) return;
  var PAGE = 100, data = null, view = [], page = 1, sortKey = null, sortDir = 1;
  var $ = function (id) { return document.getElementById(id); };
  var table = $("mt-table"), info = $("mt-info"), pager = $("mt-pager");

  function text(v) {
    if (v === null || v === undefined || v === "") return "-";
    if (v === true) return "Y";
    if (v === false) return "N";
    if (Array.isArray(v)) return v.map(text).join(", ");
    if (typeof v === "number" && Number.isInteger(v)) return v.toLocaleString("en-US");
    return String(v);
  }
  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined) e.textContent = txt;
    return e;
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
    table.textContent = "";
    var h = el("tr");
    data.fields.forEach(function (f) {
      var th = el("th", "sortable", f.no + " " + f.label + (sortKey === f.key ? (sortDir > 0 ? " ▲" : " ▼") : ""));
      th.title = f.desc;
      th.addEventListener("click", function () {
        sortDir = sortKey === f.key ? -sortDir : 1;
        sortKey = f.key;
        filter();
      });
      h.appendChild(th);
    });
    table.appendChild(h);
    view.slice((page - 1) * PAGE, page * PAGE).forEach(function (r) {
      var tr = el("tr", r.included ? "" : "muted");
      data.fields.forEach(function (f) {
        var v = r[f.key], td = el("td", typeof v === "number" ? "num" : "");
        if (f.key === "series_uid" && r.included && r._kind !== "rtstruct" && r.local_status) {
          var a = el("a", "mono", text(v));
          a.href = "/runs/" + encodeURIComponent(box.dataset.run) + "/series/" + encodeURIComponent(v);
          td.appendChild(a);
        } else {
          td.textContent = text(v);
          td.title = td.textContent;
        }
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    info.textContent = view.length.toLocaleString("en-US") + " / " + data.rows.length.toLocaleString("en-US") + " 시리즈";
    pager.textContent = "";
    var pages = Math.max(1, Math.ceil(view.length / PAGE));
    for (var i = 1; pages > 1 && i <= pages; i++) {
      (function (i) {
        var a = el(i === page ? "span" : "a", "", String(i));
        if (i !== page) {
          a.href = "#s6";
          a.addEventListener("click", function (e) { e.preventDefault(); page = i; render(); });
        }
        pager.appendChild(a);
      })(i);
    }
  }

  $("mt-controls").addEventListener("submit", function (e) { e.preventDefault(); });
  ["mt-split", "mt-kind", "mt-inc"].forEach(function (id) { $(id).addEventListener("change", filter); });
  var t = null;
  $("mt-q").addEventListener("input", function () { clearTimeout(t); t = setTimeout(filter, 200); });
  fetch(box.dataset.src, { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (d) { data = d; filter(); })
    .catch(function (e) { info.textContent = "표를 불러오지 못했습니다: " + e.message; });
})();
