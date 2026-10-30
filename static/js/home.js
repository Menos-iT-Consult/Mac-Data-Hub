// ── Bundle tab filter ────────────────────────────────────────
const tabs  = document.querySelectorAll('.tab');
const cards = document.querySelectorAll('.bundle-card');

function filterNet(net) {
  tabs.forEach(t => t.classList.toggle('active', t.dataset.net === net));
  cards.forEach(c => { c.style.display = (net === 'all' || c.dataset.net === net) ? '' : 'none'; });
}

tabs.forEach(tab => tab.addEventListener('click', () => filterNet(tab.dataset.net)));

const urlParam = new URLSearchParams(location.search).get('net');
if (urlParam) filterNet(urlParam);

// ── Order modal ──────────────────────────────────────────────
// Exposed on window so onclick= attributes in HTML can call them
window.openOrderModal  = openOrderModal;

const modal        = document.getElementById('order-modal');
const modalTitle   = document.getElementById('order-modal-title');
const backdrop     = document.getElementById('order-modal-backdrop');
const closeBtn     = document.getElementById('order-modal-close');
const pkgSelect    = document.getElementById('om-package');
const priceDisplay = document.getElementById('om-price-display');
const priceValue   = document.getElementById('om-price-value');
const errEl        = document.getElementById('om-error');
const payBtn       = document.getElementById('om-pay-btn');
const form         = document.getElementById('order-form');
let currentNetwork = '';

// MTN beneficiary verification
const verifier = (modal && window.createBeneficiaryVerifier) ? window.createBeneficiaryVerifier({
  phoneEl: document.getElementById('om-phone'),
  statusEl: document.getElementById('om-verify'),
  getBundleId: () => pkgSelect.value,
  getNetwork: () => currentNetwork,
  onChange: syncPayBtn,
}) : null;

function syncPayBtn() {
  if (!verifier) return;
  const blocked = !verifier.isVerified();
  payBtn.disabled = blocked;
  payBtn.style.opacity = blocked ? '.55' : '';
}

// Wire up close/backdrop only when modal exists
if (modal) {
  backdrop.addEventListener('click', closeOrderModal);
  closeBtn.addEventListener('click', closeOrderModal);
  pkgSelect.addEventListener('change', () => { updatePrice(); if (verifier) verifier.recheck(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeOrderModal(); });
  form.addEventListener('submit', handleSubmit);
}

function openOrderModal(network, title, preselectedId) {
  if (!modal) return;

  modalTitle.textContent = title;
  currentNetwork = network;

  // Build package options for this network
  const nets = (window.BUNDLES || []).filter(b => b.network === network);
  pkgSelect.innerHTML = '<option value="">Select package</option>';

  if (nets.length === 0) {
    const opt = document.createElement('option');
    opt.disabled = true;
    opt.textContent = 'No bundles available';
    pkgSelect.appendChild(opt);
  } else {
    nets.forEach(b => {
      const vol = b.volume_mb >= 1000
        ? (b.volume_mb / 1000).toFixed(0) + 'GB'
        : b.volume_mb + 'MB';
      const ghs = (b.price_pesewas / 100).toFixed(2);
      const opt = document.createElement('option');
      opt.value = b.id;
      opt.dataset.price = b.price_pesewas;
      opt.textContent = `${vol} — GHS ${ghs} (${b.validity_days}d)`;
      pkgSelect.appendChild(opt);
    });
  }

  // Pre-select if tapped from a specific bundle card
  if (preselectedId) {
    pkgSelect.value = preselectedId;
    updatePrice();
  } else {
    priceDisplay.classList.add('hidden');
  }

  errEl.classList.add('hidden');
  payBtn.textContent = 'Proceed to Payment';
  if (verifier) verifier.recheck();
  syncPayBtn();

  modal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  setTimeout(() => document.getElementById('om-phone').focus(), 80);
}

function closeOrderModal() {
  if (!modal) return;
  modal.classList.add('hidden');
  document.body.style.overflow = '';
  form.reset();
  priceDisplay.classList.add('hidden');
  errEl.classList.add('hidden');
}

function updatePrice() {
  const opt = pkgSelect.options[pkgSelect.selectedIndex];
  if (opt && opt.dataset.price) {
    priceValue.textContent = 'GHS ' + (parseInt(opt.dataset.price) / 100).toFixed(2);
    priceDisplay.classList.remove('hidden');
  } else {
    priceDisplay.classList.add('hidden');
  }
}

async function handleSubmit(e) {
  e.preventDefault();
  errEl.classList.add('hidden');

  const phone    = document.getElementById('om-phone').value.trim();
  const email    = document.getElementById('om-email').value.trim();
  const bundleId = pkgSelect.value;
  const storeId  = document.getElementById('om-store-id').value || null;

  if (!/^0[235]\d{8}$/.test(phone)) {
    showErr('Enter a valid 10-digit Ghana mobile number (e.g. 0541234567).');
    return;
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    showErr('Enter a valid email address for your receipt.');
    return;
  }
  if (!bundleId) {
    showErr('Please select a data package.');
    return;
  }
  if (verifier && !verifier.isVerified()) {
    showErr(verifier.isChecking() ? 'Please wait while we verify the number.'
      : (verifier.message() || 'This number is not verified to receive MTN data.'));
    return;
  }

  payBtn.disabled = true;
  payBtn.textContent = 'Please wait…';

  try {
    const resp = await fetch('/checkout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bundle_id: bundleId, store_id: storeId, phone, email }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Something went wrong. Please try again.');
    window.location.href = data.authorization_url;
  } catch (err) {
    showErr(err.message);
    payBtn.textContent = 'Proceed to Payment';
    syncPayBtn();
  }
}

function showErr(msg) {
  errEl.textContent = msg;
  errEl.classList.remove('hidden');
}
