// MTN beneficiary verification (GigzHub). Shared by storefront modal and checkout page.
// Usage: const v = createBeneficiaryVerifier({phoneEl, statusEl, getBundleId, getNetwork, onChange});
//        v.isVerified() -> true when no check needed or the current number+bundle passed.
window.createBeneficiaryVerifier = function (opts) {
  const { phoneEl, statusEl, getBundleId, getNetwork, onChange } = opts;
  let timer = null, seq = 0, state = 'idle'; // idle | checking | verified | failed

  function needsCheck() { return getNetwork() === 'mtn'; }

  function render(newState, msg) {
    state = newState;
    if (!needsCheck() || newState === 'idle') {
      statusEl.classList.add('hidden'); statusEl.textContent = '';
    } else {
      statusEl.className = 'verify-status verify-' + newState;
      statusEl.textContent = newState === 'checking' ? 'Checking…'
        : newState === 'verified' ? '✓ Verified'
        : '✕ ' + (msg || 'Not verified');
    }
    if (onChange) onChange();
  }

  async function run() {
    const phone = phoneEl.value.trim();
    const bundleId = getBundleId();
    if (!needsCheck() || !/^0[235]\d{8}$/.test(phone) || !bundleId) { render('idle'); return; }
    const mine = ++seq;
    render('checking');
    try {
      const resp = await fetch('/check-beneficiary', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, bundle_id: bundleId }),
      });
      const data = await resp.json().catch(() => ({}));
      if (mine !== seq) return; // stale response
      if (resp.ok && data.verified) render('verified');
      else render('failed', data.error || 'Not verified');
    } catch (e) {
      if (mine !== seq) return;
      render('failed', 'Could not verify this number. Check your connection and try again.');
    }
  }

  function schedule() {
    clearTimeout(timer);
    seq++;                       // invalidate in-flight checks
    render('idle');
    timer = setTimeout(run, 400);
  }

  phoneEl.addEventListener('input', schedule);

  return {
    recheck: schedule,
    isVerified() { return !needsCheck() || state === 'verified'; },
    isChecking() { return state === 'checking'; },
    message() { return statusEl.textContent.replace(/^✕\s*/, ''); },
  };
};
