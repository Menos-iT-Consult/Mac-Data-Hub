// Live profit preview
document.querySelectorAll('.price-input').forEach(input => {
  const base = parseInt(input.dataset.base, 10);
  const bundleId = input.name;
  const profitCell = document.getElementById('profit-' + bundleId);

  function update() {
    const ghs = parseFloat(input.value) || 0;
    const pesewas = Math.round(ghs * 100);
    const profit = pesewas - base;
    if (profitCell) {
      profitCell.textContent = profit >= 0 ? 'GHS ' + (profit / 100).toFixed(2) : '—';
      profitCell.style.color = profit > 0 ? 'var(--green)' : 'var(--red)';
    }
  }
  input.addEventListener('input', update);
  update();
});

// Save
const form   = document.getElementById('pricing-form');
const errEl  = document.getElementById('pricing-error');
const saveBtn = document.getElementById('save-btn');
const endpoint = form.dataset.endpoint || '/dashboard/pricing';

form.addEventListener('submit', async e => {
  e.preventDefault();
  errEl.classList.add('hidden');
  const payload = {};
  let tooLow = false;
  document.querySelectorAll('.price-input').forEach(input => {
    const ghs    = parseFloat(input.value) || 0;
    const pesewas = Math.round(ghs * 100);
    const base   = parseInt(input.dataset.base, 10);
    if (pesewas < base) { tooLow = true; return; }
    payload[input.name] = ghs.toFixed(2);  // send GHS float string — route does * 100
  });
  if (tooLow) {
    errEl.textContent = 'The price cannot be lower than the base price.';
    errEl.classList.remove('hidden');
    return;
  }

  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  try {
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'Failed to save.');
    saveBtn.textContent = 'Saved!';
    setTimeout(() => { saveBtn.disabled = false; saveBtn.textContent = 'Save prices'; }, 2000);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save prices';
  }
});
