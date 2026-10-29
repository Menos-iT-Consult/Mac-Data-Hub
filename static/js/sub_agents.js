(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  function open(id)  { $(id).classList.remove('hidden'); }
  function close(el) { el.closest('.admin-modal').classList.add('hidden'); }
  $$('[data-close]').forEach(b => b.addEventListener('click', () => close(b)));

  async function post(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify(body || {}),
      });
      const data = await r.json().catch(() => ({}));
      return { ok: r.ok && data.ok !== false, data };
    } catch (e) {
      return { ok: false, data: { error: 'Network error. Please try again.' } };
    }
  }

  // Search
  const search = $('#sa-search');
  if (search) search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    $$('.sa-card').forEach(c => {
      const hit = !q || c.dataset.search.includes(q);
      c.classList.toggle('hidden', !hit);
      if (hit) shown++;
    });
    $('#sa-no-match').classList.toggle('hidden', shown > 0);
  });

  // Add sub-agent
  const addForm = $('#sa-add-form');
  $('#sa-add-btn').addEventListener('click', () => { addForm.reset(); $('#sa-add-error').classList.add('hidden'); open('#sa-add-modal'); });
  $('#sa-gen-pw').addEventListener('click', () => {
    const a = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
    const buf = new Uint32Array(10); crypto.getRandomValues(buf);
    $('#sa-password').value = Array.from(buf, n => a[n % a.length]).join('');
  });
  $('#sa-username').addEventListener('input', e => e.target.value = e.target.value.replace(/\s/g, '').toLowerCase());
  $('#sa-slug').addEventListener('input', e => e.target.value = e.target.value.replace(/\s/g, '-').toLowerCase());

  let credReload = false;
  function showCreds(title, d) {
    $('#sa-cred-title').textContent = title;
    $('#sa-cred-box').textContent =
      `Login: ${d.login_url || location.origin + '/login'}\nUsername: ${d.username}\nEmail: ${d.email}\nPassword: ${d.password}`;
    credReload = true;
    open('#sa-cred-modal');
  }
  $('#sa-cred-copy').addEventListener('click', async e => {
    try { await navigator.clipboard.writeText($('#sa-cred-box').textContent); e.target.textContent = 'Copied'; }
    catch (_) { e.target.textContent = 'Select & copy'; }
  });
  // Reload the list once the credentials dialog is dismissed
  $('#sa-cred-modal').addEventListener('click', e => {
    if (credReload && e.target.hasAttribute('data-close')) location.reload();
  });

  addForm.addEventListener('submit', async e => {
    e.preventDefault();
    const err = $('#sa-add-error'); err.classList.add('hidden');
    const btn = $('#sa-add-submit'); btn.disabled = true;
    const { ok, data } = await post('/dashboard/sub-agents/create', {
      full_name: $('#sa-name').value, phone: $('#sa-phone').value, email: $('#sa-email').value,
      username: $('#sa-username').value, slug: $('#sa-slug').value, password: $('#sa-password').value,
    });
    btn.disabled = false;
    if (!ok) { err.textContent = data.error || 'Could not create sub-agent.'; err.classList.remove('hidden'); return; }
    close(addForm);
    showCreds('Sub-agent created', data);
  });

  // Row actions
  let pending = null;
  function confirmBox(title, text, label, fn) {
    $('#sa-confirm-title').textContent = title;
    $('#sa-confirm-text').textContent = text;
    $('#sa-confirm-ok').textContent = label;
    $('#sa-confirm-error').classList.add('hidden');
    pending = fn;
    open('#sa-confirm-modal');
  }
  $('#sa-confirm-ok').addEventListener('click', async () => {
    const btn = $('#sa-confirm-ok'); btn.disabled = true;
    const err = $('#sa-confirm-error');
    const res = pending ? await pending() : null;
    btn.disabled = false;
    if (res && res.error) { err.textContent = res.error; err.classList.remove('hidden'); }
  });

  $$('.sa-card [data-act]').forEach(btn => btn.addEventListener('click', () => {
    const card = btn.closest('.sa-card');
    const id = card.dataset.id, name = card.dataset.name, act = btn.dataset.act;
    const base = `/dashboard/sub-agents/${id}/${act}`;

    if (act === 'suspend') {
      const suspended = btn.dataset.suspended === '1';
      confirmBox(suspended ? 'Unsuspend account' : 'Suspend account',
        suspended ? `${name} will be able to log in and sell again.`
                  : `${name} will be signed out, unable to log in, and their store will be hidden.`,
        suspended ? 'Unsuspend' : 'Suspend',
        async () => { const r = await post(base); if (r.ok) location.reload(); else return r.data; });
    } else if (act === 'block-withdrawals') {
      const blocked = btn.dataset.blocked === '1';
      confirmBox(blocked ? 'Allow withdrawals' : 'Block withdrawals',
        blocked ? `${name} will be able to withdraw from their wallet again.`
                : `${name} can keep selling but won't be able to withdraw.`,
        blocked ? 'Allow' : 'Block',
        async () => { const r = await post(base); if (r.ok) location.reload(); else return r.data; });
    } else if (act === 'reset-password') {
      confirmBox('Reset password', `Generate a new password for ${name}? Their old password stops working immediately.`, 'Reset',
        async () => {
          const r = await post(base);
          if (!r.ok) return r.data;
          close($('#sa-confirm-ok'));
          showCreds('New password', { ...r.data, login_url: location.origin + '/login' });
        });
    } else if (act === 'remove') {
      confirmBox('Remove sub-agent', `Remove ${name}? They lose access and their store goes offline. Order history is kept.`, 'Remove',
        async () => { const r = await post(base); if (r.ok) location.reload(); else return r.data; });
    }
  }));
})();
