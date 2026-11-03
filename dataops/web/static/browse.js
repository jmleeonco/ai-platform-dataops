// /browse: a dropdown filter applies as soon as it changes (text filters apply on Enter). Empty filters and the
// default column choice are left out of the URL, so links stay short.
(function () {
  var form = document.getElementById("browse");
  if (!form) return;
  form.querySelectorAll("select.autosubmit").forEach(function (s) {
    s.addEventListener("change", function () { form.requestSubmit(); });
  });
  form.addEventListener("submit", function () {
    form.querySelectorAll("input[name^='f_'], select[name^='f_'], input[name='q']").forEach(function (e) {
      if (!e.value.trim()) e.disabled = true;
    });
    var boxes = Array.prototype.slice.call(form.querySelectorAll("input[name='c']"));
    if (boxes.every(function (b) { return b.checked === b.hasAttribute("data-default"); }))
      boxes.forEach(function (b) { b.disabled = true; });
  });
})();
