// Report tables fetched as JSON (6 DICOM, 7 DB tables, 8 source values): search, a filter per listed column,
// sorting and paging. Containers sharing a data-src share one fetch; data-refresh re-fetches every N seconds
// (table 7: each row's state in the live DB) and keeps the reader's search, filters, sort and page.
// Values are dataset text: always set with textContent, never as HTML.
(function () {
  var boxes = document.querySelectorAll(".jt");
  if (!boxes.length) return;
  var PAGE = 50, CUT = 120;

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
  function clock(iso) {
    var d = iso ? new Date(iso) : new Date();
    return isNaN(d) ? String(iso) : d.toLocaleTimeString("ko-KR", { hour12: false });
  }
  var BADGE = { "신규": "b-new", "같음": "b-ok", "변경": "b-warn", "충돌": "b-bad", "일치": "b-ok" };

  function makeTable(box) {
    var data = null, view = [], page = 1, sortKey = null, sortDir = 1, sel = {};
    box.textContent = "";
    var form = el("form", "inline"), q = el("input");
    q.type = "search";
    q.placeholder = "검색";
    form.appendChild(q);
    var selects = el("span");
    form.appendChild(selects);
    if (box.dataset.csv) {
      var a = el("a", "button", "CSV");
      a.href = box.dataset.csv;
      form.appendChild(a);
    }
    form.addEventListener("submit", function (e) { e.preventDefault(); });
    var info = el("p", "muted small jt-info", "불러오는 중…"), wrap = el("div", "table-wrap jt-wrap"),
        table = el("table", "std"), pager = el("p", "pager");
    wrap.appendChild(table);
    [form, info, wrap, pager].forEach(function (x) { box.appendChild(x); });
    var t = null;
    q.addEventListener("input", function () { clearTimeout(t); t = setTimeout(function () { page = 1; apply(); }, 200); });

    function label(key) {
      var c = data.columns.filter(function (c) { return c.key === key; })[0];
      return c ? c.label : key;
    }
    function buildFilters() {
      selects.textContent = "";
      (data.filters || []).forEach(function (key) {
        var values = {}, s = el("select");
        data.rows.forEach(function (r) { values[text(r[key])] = 1; });
        s.appendChild(el("option", "", label(key) + " 전체")).value = "";
        Object.keys(values).sort().forEach(function (v) { s.appendChild(el("option", "", v)).value = v; });
        s.value = sel[key] && values[sel[key]] ? sel[key] : "";
        s.addEventListener("change", function () { sel[key] = s.value; page = 1; apply(); });
        selects.appendChild(s);
      });
    }
    function apply() {
      var needle = q.value.trim().toLowerCase();
      view = data.rows.filter(function (r) {
        for (var k in sel) if (sel[k] && text(r[k]) !== sel[k]) return false;
        if (!needle) return true;
        for (var c in r) if (text(r[c]).toLowerCase().indexOf(needle) >= 0) return true;
        return false;
      });
      if (sortKey) {
        view.sort(function (a, b) {
          var x = a[sortKey], y = b[sortKey];
          if (typeof x === "number" && typeof y === "number") return (x - y) * sortDir;
          return text(x).localeCompare(text(y), "ko", { numeric: true }) * sortDir;
        });
      }
      render();
    }
    function render() {
      table.textContent = "";
      var h = el("tr");
      data.columns.forEach(function (c) {
        var th = el("th", "sortable", c.label + (sortKey === c.key ? (sortDir > 0 ? " ▲" : " ▼") : ""));
        th.title = c.desc || "";
        th.addEventListener("click", function () {
          sortDir = sortKey === c.key ? -sortDir : 1;
          sortKey = c.key;
          apply();
        });
        h.appendChild(th);
      });
      table.appendChild(h);
      var pages = Math.max(1, Math.ceil(view.length / PAGE));
      if (page > pages) page = pages;
      view.slice((page - 1) * PAGE, page * PAGE).forEach(function (r) {
        var tr = el("tr", r.included === false ? "muted" : "");
        data.columns.forEach(function (c) {
          var v = r[c.key], s = text(v), td = el("td", typeof v === "number" ? "num" : "");
          var badge = BADGE[s] || (s.indexOf("불일치") === 0 ? "b-bad" : "");
          if ((c.key === "series_uid" || c.key === "SeriesInstanceUID") && box.dataset.run && r.local_status &&
              r._kind !== "rtstruct") {
            var a = el("a", "mono", s);
            a.href = "/runs/" + encodeURIComponent(box.dataset.run) + "/series/" + encodeURIComponent(v);
            td.appendChild(a);
          } else if (badge) {
            td.appendChild(el("span", "badge " + badge, s));
          } else {
            td.textContent = s.length > CUT ? s.slice(0, CUT) + " …" : s;
          }
          td.title = s;
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      if (!view.length) {
        var none = el("td", "muted", data.rows.length ? "조건에 맞는 행 없음" : "행 없음");
        none.colSpan = data.columns.length;
        table.appendChild(el("tr")).appendChild(none);
      }
      info.textContent = view.length.toLocaleString("en-US") + " / " + data.rows.length.toLocaleString("en-US") + "행" +
        (data.note ? " · " + data.note : "");
      pager.textContent = "";
      for (var i = 1; pages > 1 && i <= pages; i++) {
        if (!(i <= 3 || i > pages - 3 || Math.abs(i - page) < 3)) continue;
        (function (i) {
          var a = el(i === page ? "span" : "a", "", String(i));
          if (i !== page) {
            a.href = "#" + box.id;
            a.addEventListener("click", function (e) { e.preventDefault(); page = i; render(); });
          }
          pager.appendChild(a);
        })(i);
      }
    }
    return {
      box: box,
      set: function (d) { data = d; buildFilters(); apply(); },
      error: function (msg) { info.textContent = "표를 불러오지 못했습니다: " + msg; }
    };
  }

  // Table 7 summary: live counts per table and DB state.
  function summary(d) {
    if (!d.tables) return;
    Object.keys(d.tables).forEach(function (t) {
      ["신규", "같음", "변경", "충돌"].forEach(function (st) {
        var c = document.getElementById("c7-" + t + "-" + st);
        if (c) c.textContent = (d.tables[t].counts[st] || 0).toLocaleString("en-US");
      });
    });
    var b = document.getElementById("c7-checked");
    if (b) {
      b.textContent = (d.db === "ok" ? "DB 확인 " : "DB 비교 안 됨: " + d.db + " · ") + clock(d.checked_at);
      b.className = "badge " + (d.db === "ok" ? "b-ok" : "b-warn");
    }
  }

  var groups = {};
  Array.prototype.forEach.call(boxes, function (box) {
    (groups[box.dataset.src] = groups[box.dataset.src] || []).push(makeTable(box));
  });
  Object.keys(groups).forEach(function (src) {
    var list = groups[src];
    var every = Math.max.apply(null, list.map(function (t) { return +t.box.dataset.refresh || 0; }));
    function load() {
      fetch(src, { cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (d) {
          list.forEach(function (t) { t.set(t.box.dataset.table ? d.tables[t.box.dataset.table] : d); });
          summary(d);
        })
        .catch(function (e) { list.forEach(function (t) { t.error(e.message); }); })
        .then(function () { if (every) setTimeout(load, every * 1000); });
    }
    load();
  });
})();
