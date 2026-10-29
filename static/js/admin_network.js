(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const open = id => $(id).classList.remove('hidden');
  const close = el => el.closest('.admin-modal').classList.add('hidden');
  $$('[data-close]').forEach(b => b.addEventListener('click', () => close(b)));

  async function post(url) {
    try {
      const r = await fetch(url, { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json' }, body: '{}' });
      const d = await r.json().catch(() => ({}));
      return { ok: r.ok && d.ok !== false, data: d };
    } catch (e) { return { ok: false, data: { error: 'Network error. Please try again.' } }; }
  }

  let pending = null;
  function confirmBox(title, text, label, fn) {
    $('#net-confirm-title').textContent = title;
    $('#net-confirm-text').textContent = text;
    $('#net-confirm-ok').textContent = label;
    $('#net-confirm-error').classList.add('hidden');
    pending = fn; open('#net-confirm-modal');
  }
  $('#net-confirm-ok').addEventListener('click', async () => {
    const btn = $('#net-confirm-ok'); btn.disabled = true;
    const res = pending ? await pending() : null;
    btn.disabled = false;
    if (res && res.error) { const e = $('#net-confirm-error'); e.textContent = res.error; e.classList.remove('hidden'); }
  });
  $('#net-cred-copy').addEventListener('click', async e => {
    try { await navigator.clipboard.writeText($('#net-cred-box').textContent); e.target.textContent = 'Copied'; }
    catch (_) { e.target.textContent = 'Select & copy'; }
  });

  const reload = async (url, el) => { const r = await post(url); if (r.ok) { const d = el.closest('.net-agent'); if (d) sessionStorage.setItem('net-open', d.dataset.agent); location.reload(); } else return r.data; };

  $$('.net-ctrl [data-act]').forEach(btn => btn.addEventListener('click', () => {
    const box = btn.closest('.net-ctrl');
    const id = box.dataset.user, name = box.dataset.name, act = btn.dataset.act;
    const url = `/admin/network/${id}/${act}`;
    const on = btn.dataset.state === '1';
    if (act === 'suspend') {
      confirmBox(on ? 'Unsuspend account' : 'Suspend account',
        on ? `${name} will be able to log in and sell again.` : `${name} will be signed out, unable to log in, and their store will be hidden.`,
        on ? 'Unsuspend' : 'Suspend', () => reload(url, btn));
    } else if (act === 'block-withdrawals') {
      confirmBox(on ? 'Allow withdrawals' : 'Block withdrawals',
        on ? `${name} will be able to withdraw again.` : `${name} can keep selling but won't be able to withdraw.`,
        on ? 'Allow' : 'Block', () => reload(url, btn));
    } else if (act === 'reset-password') {
      confirmBox('Reset password', `Generate a new password for ${name}? Their old password stops working immediately.`, 'Reset', async () => {
        const r = await post(url);
        if (!r.ok) return r.data;
        close($('#net-confirm-ok'));
        $('#net-cred-box').textContent = `Login: ${location.origin}/login\nUsername: ${r.data.username}\nEmail: ${r.data.email}\nPassword: ${r.data.password}`;
        open('#net-cred-modal');
      });
    } else if (act === 'remove') {
      confirmBox('Remove sub-agent', `Remove ${name}? They lose access and their store goes offline. Order history is kept, and their email and username are freed up.`, 'Remove', () => reload(url, btn));
    }
  }));

  // Keep agents expanded after an action reloads the page
  const openId = sessionStorage.getItem('net-open');
  if (openId) {
    sessionStorage.removeItem('net-open');
    const d = document.querySelector(`.net-agent[data-agent="${openId}"]`);
    if (d) d.open = true;
  }
})();
