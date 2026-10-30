(function () {
  "use strict";

  var form     = document.getElementById("track-form");
  var phoneEl  = document.getElementById("track-phone");
  var trackBtn = document.getElementById("track-btn");
  var results  = document.getElementById("track-results");

  if (!form) return;

  var STATUS_LABELS = {
    pending:    { label: "Awaiting payment", cls: "status-pending"    },
    paid:       { label: "Processing",       cls: "status-processing" },
    dispatched: { label: "Dispatched",       cls: "status-dispatched" },
    processing: { label: "Processing",       cls: "status-processing" },
    completed:  { label: "Completed",        cls: "status-completed"  },
    failed:     { label: "Failed",           cls: "status-failed"     },
  };

  function fmtDate(iso) {
    var d = new Date(iso.replace(" ", "T"));
    return d.toLocaleDateString("en-GH", { day: "numeric", month: "short", year: "numeric" })
      + " " + d.toLocaleTimeString("en-GH", { hour: "2-digit", minute: "2-digit" });
  }

  function fmtVol(mb) {
    return mb >= 1000 ? (mb / 1000).toFixed(mb % 1000 === 0 ? 0 : 1) + "GB" : mb + "MB";
  }

  function renderOrders(orders, phone) {
    var sub = document.querySelector(".order-modal-sub");
    if (sub && document.getElementById("track-modal") &&
        !document.getElementById("track-modal").classList.contains("hidden")) {
      sub.textContent = "Showing orders for " + phone;
    }

    if (!orders.length) {
      results.innerHTML =
        '<div class="track-empty">' +
        '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' +
        '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>' +
        "<div>No orders found for <strong>" + phone + "</strong>.</div>" +
        '<div style="font-size:.8rem;margin-top:.3rem">Make sure you enter the exact number used during checkout.</div>' +
        "</div>";
      return;
    }

    results.innerHTML = orders.map(function (o) {
      var st  = STATUS_LABELS[o.status] || { label: o.status, cls: "status-pending" };
      var net = (o.network || "").toLowerCase();
      var amt = "GHS " + (o.amount_pesewas / 100).toFixed(2);
      return (
        '<div class="track-order-item">' +
        '<div class="track-order-net net-' + net + '">' +
          (o.network ? o.network.toUpperCase().slice(0, 3) : "?") +
        "</div>" +
        '<div class="track-order-info">' +
          '<div class="track-order-label">' + (o.label || fmtVol(o.volume_mb)) + "</div>" +
          '<div class="track-order-meta">' +
            '<span class="status-pill ' + st.cls + '" style="font-size:.7rem;padding:.2rem .55rem">' + st.label + "</span>" +
          "</div>" +
        "</div>" +
        '<div class="track-order-right">' +
          '<div class="track-order-amount">' + amt + "</div>" +
          '<div class="track-order-date">' + fmtDate(o.created_at) + "</div>" +
        "</div>" +
        "</div>"
      );
    }).join("");
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();

    var phone = phoneEl ? phoneEl.value.trim() : "";
    if (!phone) return;

    trackBtn.disabled = true;
    trackBtn.innerHTML =
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="animation:spin .7s linear infinite">' +
      '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-.49-5"/></svg> Searching…';

    var storeId = window.TRACK_STORE_ID;
    var url = "/track?phone=" + encodeURIComponent(phone) +
      (storeId ? "&store_id=" + encodeURIComponent(storeId) : "");

    fetch(url)
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "Could not fetch orders.");
        if (results) results.classList.remove("hidden");
        renderOrders(data.orders, phone);
      })
      .catch(function (err) {
        if (results) {
          results.innerHTML =
            '<div class="track-empty" style="color:var(--red)">' + err.message + "</div>";
          results.classList.remove("hidden");
        }
      })
      .finally(function () {
        trackBtn.disabled = false;
        trackBtn.innerHTML =
          '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">' +
          '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Track';
      });
  });
})();
