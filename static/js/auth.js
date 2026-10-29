/* ── Password reveal toggle ──────────────────────────────── */
function togglePw(id, btn) {
  var inp = document.getElementById(id);
  var show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  btn.querySelector('.pw-eye').style.display     = show ? 'none' : '';
  btn.querySelector('.pw-eye-off').style.display = show ? ''     : 'none';
}

/* ── Register form ───────────────────────────────────────── */
(function () {
  var form   = document.getElementById('register-form');
  if (!form) return;

  var errEl  = document.getElementById('form-error');
  var regBtn = document.getElementById('register-btn');

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    errEl.classList.add('hidden');

    var password = document.getElementById('password').value;
    var confirm  = document.getElementById('confirm_password').value;
    if (password !== confirm) {
      errEl.textContent = 'Passwords do not match.';
      errEl.classList.remove('hidden');
      return;
    }

    var usernameEl = document.getElementById('username');
    var username = usernameEl ? usernameEl.value.replace(/\s/g, '').toLowerCase() : '';
    if (!username) {
      errEl.textContent = 'Username is required.';
      errEl.classList.remove('hidden');
      return;
    }
    if (!/^[a-z0-9][a-z0-9_-]{1,28}[a-z0-9]$/.test(username) && !/^[a-z0-9]{3}$/.test(username)) {
      errEl.textContent = 'Username must be 3–30 characters, start and end with a letter or number, no spaces.';
      errEl.classList.remove('hidden');
      return;
    }
    if (/[_-]{2,}/.test(username)) {
      errEl.textContent = 'Username cannot have consecutive hyphens or underscores (e.g. -- or __).';
      errEl.classList.remove('hidden');
      return;
    }

    var slug = document.getElementById('slug').value.trim();
    if (!/^[a-z0-9-]+$/.test(slug)) {
      errEl.textContent = 'Store URL can only contain lowercase letters, numbers and hyphens.';
      errEl.classList.remove('hidden');
      return;
    }

    regBtn.disabled    = true;
    regBtn.textContent = 'Please wait…';

    try {
      var resp = await fetch('/register', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          full_name: document.getElementById('full_name').value.trim(),
          phone:     document.getElementById('phone').value.trim(),
          email:     document.getElementById('email').value.trim(),
          username:  username,
          slug:      slug,
          referral_code: (document.getElementById('referral_code') || {}).value || '',
          password:  password,
        }),
      });
      var data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'Something went wrong.');
      // Free registration returns a redirect URL; paid returns a Paystack authorization_url
      window.location.href = data.redirect || data.authorization_url;
    } catch (err) {
      errEl.textContent  = err.message;
      errEl.classList.remove('hidden');
      regBtn.disabled    = false;
      regBtn.textContent = 'Try again';
    }
  });
})();
