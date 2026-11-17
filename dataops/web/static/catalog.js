// Catalogue filters: ticking a value applies it at once (the form still works without this script).
(function () {
  var form = document.getElementById("catalog-filters");
  if (!form) return;
  form.addEventListener("change", function (ev) {
    if (ev.target && ev.target.type === "checkbox") form.submit();
  });
})();
