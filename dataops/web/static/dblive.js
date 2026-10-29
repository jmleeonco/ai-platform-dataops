// DB pages: every few seconds ask /db/state.json for the change marks (row counts on the overview, live table
// checksums on table and row pages) of the tables this page shows; when they
// differ from the ones the page was rendered with, fetch the same URL again and swap in its #dblive content
// (server-rendered and escaped, same origin). Pauses while the tab is hidden or the reader presses pause.
(function () {
  var box = document.getElementById("dblive");
  var status = document.getElementById("dblive-status"), pause = document.getElementById("dblive-pause");
  if (!box || !status || !box.dataset.tables) return;
  var tables = box.dataset.tables.split(","), sig = box.dataset.sig, every = (+box.dataset.every || 5) * 1000;
  var url = "/db/state.json?t=" + encodeURIComponent(box.dataset.tables) + "&exact=" + (box.dataset.exact || "0");
  var paused = false, busy = false;

  function now() { return new Date().toLocaleTimeString("ko-KR", { hour12: false }); }
  function show(text, cls) { status.textContent = text; status.className = "badge " + cls; }
  pause.addEventListener("click", function () {
    paused = !paused;
    pause.textContent = paused ? "다시 시작" : "일시 정지";
    show(paused ? "실시간 정지됨" : "실시간 · 다시 시작", paused ? "b-warn" : "b-ok");
  });

  function reload(next) {
    return fetch(location.href, { cache: "no-store" })
      .then(function (r) {
        if (r.status === 404) throw new Error("이 행/테이블이 DB에서 없어졌습니다");
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("dblive");
        if (!fresh) throw new Error("화면을 다시 그리지 못했습니다");
        var node = document.importNode(fresh, true);
        box.replaceWith(node);
        box = node;
        sig = next;
        show("실시간 · " + now() + " 변경 반영", "b-ok");
      });
  }
  function tick() {
    if (paused || busy || document.hidden) return;
    busy = true;
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) { show("실시간 · DB 연결 실패: " + d.error, "b-bad"); return; }
        var next = tables.map(function (t) { return d.marks[t] || ""; }).join(";");
        if (next !== sig) return reload(next);
        show("실시간 · " + now() + " 확인", "b-ok");
      })
      .catch(function (e) { show("실시간 · " + e.message, "b-bad"); })
      .then(function () { busy = false; });
  }
  setInterval(tick, every);
})();
