/* Keeps order status pills current without a page reload.
   Put data-status-url="..." on the #orders-table and data-order-id="..." on each row.
   The server (not a person) decides the status, based on what GigzHub reports. */
(function () {
  "use strict";
  var table = document.getElementById("orders-table");
  if (!table || !table.dataset.statusUrl) return;

  var LABELS = {
    pending: "Awaiting payment", paid: "Processing", processing: "Processing",
    dispatched: "Dispatched", completed: "Completed", failed: "Failed"
  };
  var timer = null;

  function apply(statuses) {
    table.querySelectorAll("tbody tr[data-order-id]").forEach(function (tr) {
      var st = statuses[tr.dataset.orderId];
      if (!st || st === tr.dataset.status) return;
      tr.dataset.status = st;
      var pill = tr.querySelector(".status-pill");
      if (pill) { pill.className = "status-pill status-" + st; pill.textContent = LABELS[st] || st; }
      var retry = tr.querySelector(".retry-btn");
      if (retry) retry.style.display = (st === "failed" || st === "paid") ? "" : "none";
    });
  }

  function tick() {
    if (document.hidden) return;
    fetch(table.dataset.statusUrl, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d && d.statuses) apply(d.statuses); })
      .catch(function () {});
  }

  timer = setInterval(tick, 30000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
})();
