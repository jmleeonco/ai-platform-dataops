// Run page: while a job is queued/running, poll the run state and reload when it changes.
(function () {
  var live = document.getElementById("live");
  if (!live || !live.dataset.active) return;
  var run = live.dataset.run, status = live.dataset.status, log = document.getElementById("joblog");
  function tick() {
    fetch("/runs/" + encodeURIComponent(run) + "/state.json", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.status !== status || !s.job) { location.reload(); return; }
        if (log && s.job.log) log.textContent = s.job.log.join("\n");
        setTimeout(tick, 3000);
      })
      .catch(function () { setTimeout(tick, 10000); });
  }
  setTimeout(tick, 3000);
})();
