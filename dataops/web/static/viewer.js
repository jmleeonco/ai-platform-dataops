// Slice viewer for prepared artifacts: slider, window centre/width, presets, arrow keys / wheel.
(function () {
  var root = document.getElementById("viewer");
  if (!root) return;
  var run = root.dataset.run;
  var sops = root.dataset.sops.split(",");
  var img = document.getElementById("slice");
  var idx = document.getElementById("idx");
  var pos = document.getElementById("pos");
  var wc = document.getElementById("wc");
  var ww = document.getElementById("ww");
  var preset = document.getElementById("preset");
  var msg = document.getElementById("msg");

  function url(i) {
    var u = "/runs/" + encodeURIComponent(run) + "/instances/" + encodeURIComponent(sops[i]) + ".png";
    if (wc.value !== "" && ww.value !== "" && Number(ww.value) > 0) {
      u += "?wc=" + encodeURIComponent(wc.value) + "&ww=" + encodeURIComponent(ww.value);
    }
    return u;
  }
  function show() {
    var i = Number(idx.value);
    pos.textContent = String(i + 1);
    img.src = url(i);
  }
  img.addEventListener("error", function () { msg.textContent = "이 슬라이스는 미리보기를 만들 수 없습니다 (압축/비영상 객체일 수 있음)"; });
  img.addEventListener("load", function () { msg.textContent = ""; });
  idx.addEventListener("input", show);
  wc.addEventListener("change", function () { preset.value = ""; show(); });
  ww.addEventListener("change", function () { preset.value = ""; show(); });
  preset.addEventListener("change", function () {
    if (preset.value) { var p = preset.value.split(","); wc.value = p[0]; ww.value = p[1]; }
    else { wc.value = ""; ww.value = ""; }
    show();
  });
  function step(d) {
    idx.value = String(Math.min(sops.length - 1, Math.max(0, Number(idx.value) + d)));
    show();
  }
  document.addEventListener("keydown", function (e) {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.key === "ArrowUp" || e.key === "ArrowRight") { step(1); e.preventDefault(); }
    if (e.key === "ArrowDown" || e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
  });
  img.parentElement.addEventListener("wheel", function (e) { step(e.deltaY > 0 ? 1 : -1); e.preventDefault(); }, { passive: false });
})();
