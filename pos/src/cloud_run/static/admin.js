/* POS admin portal: manage the accounts in pos_users. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let me = null;
  let users = [];
  let pinTarget = null;

  const api = async (path, options = {}) => {
    const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', ...options, body: options.body ? JSON.stringify(options.body) : undefined });
    let data = null;
    try { data = await response.json(); } catch { /* no body */ }
    if (response.status === 401) { showLogin(); throw new Error('Signed out'); }
    if (!response.ok) throw new Error((data && data.error) || `Request failed (${response.status})`);
    return data;
  };

  let toastTimer;
  const toast = (msg) => { const el = $('toast'); el.textContent = msg; el.classList.add('on'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('on'), 2600); };
  const openSheet = (id) => { $('scrim').classList.add('on'); $(id).classList.add('on'); };
  const closeSheets = () => { $('scrim').classList.remove('on'); document.querySelectorAll('.sheet').forEach((s) => s.classList.remove('on')); };

  const showLogin = () => { $('lock').hidden = false; $('admin').hidden = true; };
  const showAdmin = async () => { $('lock').hidden = true; $('admin').hidden = false; $('whoPill').textContent = me.staff; await Promise.all([loadUsers(), loadEvents()]); };

  let mode = 'user';
  const setMode = (m) => {
    mode = m;
    $('tabUser').classList.toggle('on', m === 'user');
    $('tabMaster').classList.toggle('on', m === 'master');
    $('userFields').hidden = m !== 'user';
    $('masterFields').hidden = m !== 'master';
    $('loginErr').textContent = '';
  };
  $('tabUser').addEventListener('click', () => setMode('user'));
  $('tabMaster').addEventListener('click', () => setMode('master'));

  $('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('loginErr').textContent = '';
    $('loginBtn').disabled = true;
    try {
      const body = mode === 'master' ? { code: $('loginCode').value } : { staff: $('loginName').value, pin: $('loginPin').value };
      const data = await api('/api/admin/login', { method: 'POST', body });
      me = data;
      $('loginPin').value = ''; $('loginCode').value = '';
      await showAdmin();
    } catch (err) { $('loginErr').textContent = err.message; }
    finally { $('loginBtn').disabled = false; }
  });

  $('logout').addEventListener('click', async () => { await api('/api/lock', { method: 'POST' }).catch(() => {}); me = null; showLogin(); });

  const loadUsers = async () => { users = await api('/api/admin/users'); renderUsers(); };

  const when = (iso) => (iso ? new Date(iso).toLocaleString('en-CA', { dateStyle: 'medium', timeStyle: 'short' }) : 'never');

  const renderUsers = () => {
    if (!users.length) { $('userRows').innerHTML = '<tr><td colspan="5" class="hint">No users yet. Add the first admin above.</td></tr>'; return; }
    $('userRows').innerHTML = users.map((u) => `
      <tr class="${u.isActive ? '' : 'inactive'}" data-id="${esc(u.id)}">
        <td><b>${esc(u.name)}</b></td>
        <td>${u.role === 'admin' ? '<span class="tag">admin</span>' : 'Cashier'}</td>
        <td>${esc(when(u.lastLoginAt))}</td>
        <td>${u.isActive ? '<span class="status">Active</span>' : '<span class="status refunded">Inactive</span>'}</td>
        <td class="actions">
          <button class="sm" data-act="pin">Reset PIN</button>
          <button class="sm" data-act="role">${u.role === 'admin' ? 'Make cashier' : 'Make admin'}</button>
          <button class="sm ${u.isActive ? 'danger' : ''}" data-act="toggle">${u.isActive ? 'Deactivate' : 'Reactivate'}</button>
        </td>
      </tr>`).join('');
  };

  $('userRows').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.closest('tr').dataset.id;
    const u = users.find((x) => x.id === id);
    if (!u) return;
    try {
      if (btn.dataset.act === 'pin') { pinTarget = u; $('pinName').textContent = u.name; $('pinInput').value = ''; $('pinErr').textContent = ''; openSheet('pinSheet'); $('pinInput').focus(); return; }
      if (btn.dataset.act === 'role') await api(`/api/admin/users/${id}`, { method: 'PATCH', body: { role: u.role === 'admin' ? 'cashier' : 'admin' } });
      if (btn.dataset.act === 'toggle') {
        if (u.isActive && !confirm(`Deactivate ${u.name}? They will no longer be able to sign in.`)) return;
        await api(`/api/admin/users/${id}`, { method: 'PATCH', body: { isActive: !u.isActive } });
      }
      toast(`Updated ${u.name}`);
      await loadUsers();
    } catch (err) { toast(err.message); if (err.message === 'Signed out') showLogin(); }
  });

  $('pinSave').addEventListener('click', async () => {
    if (!pinTarget) return;
    try {
      await api(`/api/admin/users/${pinTarget.id}`, { method: 'PATCH', body: { pin: $('pinInput').value } });
      closeSheets();
      toast(`PIN set for ${pinTarget.name}`);
    } catch (err) { $('pinErr').textContent = err.message; }
  });

  $('addForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('addErr').textContent = '';
    try {
      const u = await api('/api/admin/users', { method: 'POST', body: { name: $('addName').value, role: $('addRole').value, pin: $('addPin').value } });
      $('addName').value = ''; $('addPin').value = ''; $('addRole').value = 'cashier';
      toast(`Added ${u.name}`);
      await loadUsers();
    } catch (err) { $('addErr').textContent = err.message; }
  });

  // ---- events
  let events = [];
  let editing = null;
  const today = () => new Date().toISOString().slice(0, 10);
  const loadEvents = async () => { events = await api('/api/admin/events'); renderEvents(); };
  const renderEvents = () => {
    const t = today();
    $('eventRows').innerHTML = events.length ? events.map((e) => {
      const state = !e.isActive ? ['refunded', 'Archived'] : (e.startsOn <= t && t <= e.endsOn) ? ['', 'Running'] : e.startsOn > t ? ['partial', 'Upcoming'] : ['', 'Past'];
      return `<tr class="${e.isActive ? '' : 'inactive'}" data-id="${esc(e.id)}">
        <td><b>${esc(e.name)}</b><div class="hint">${esc(e.code)}</div></td>
        <td>${esc(e.startsOn)} → ${esc(e.endsOn)}</td>
        <td>${esc(e.currency)}</td>
        <td><span class="status ${state[0]}">${state[1]}</span></td>
        <td class="actions">
          <button class="sm" data-eact="edit">Edit</button>
          <button class="sm ${e.isActive ? 'danger' : ''}" data-eact="toggle">${e.isActive ? 'Archive' : 'Restore'}</button>
        </td></tr>`;
    }).join('') : '<tr><td colspan="5" class="hint">No events yet. Add one above.</td></tr>';
  };
  $('eventForm').addEventListener('submit', async (e) => {
    e.preventDefault(); $('evErr').textContent = '';
    try {
      const ev = await api('/api/admin/events', { method: 'POST', body: { name: $('evName').value, startsOn: $('evStart').value, endsOn: $('evEnd').value, currency: $('evCurrency').value } });
      $('evName').value = ''; $('evStart').value = ''; $('evEnd').value = '';
      toast(`Added ${ev.name}`); await loadEvents();
    } catch (err) { $('evErr').textContent = err.message; }
  });
  $('eventRows').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-eact]'); if (!btn) return;
    const ev = events.find((x) => x.id === btn.closest('tr').dataset.id); if (!ev) return;
    if (btn.dataset.eact === 'edit') {
      editing = ev; $('esName').textContent = ev.name; $('esNameInput').value = ev.name; $('esStart').value = ev.startsOn; $('esEnd').value = ev.endsOn; $('esCurrency').value = ev.currency; $('esErr').textContent = '';
      openSheet('eventSheet'); return;
    }
    try {
      if (ev.isActive && !confirm(`Archive ${ev.name}? It disappears from the register's selector; its sales are kept.`)) return;
      await api(`/api/admin/events/${ev.id}`, { method: 'PATCH', body: { isActive: !ev.isActive } });
      toast(`Updated ${ev.name}`); await loadEvents();
    } catch (err) { toast(err.message); }
  });
  $('esSave').addEventListener('click', async () => {
    if (!editing) return;
    try {
      await api(`/api/admin/events/${editing.id}`, { method: 'PATCH', body: { name: $('esNameInput').value, startsOn: $('esStart').value, endsOn: $('esEnd').value, currency: $('esCurrency').value } });
      closeSheets(); toast('Event updated'); await loadEvents();
    } catch (err) { $('esErr').textContent = err.message; }
  });

  // ---- products by CSV
  const sendCsv = async (preview) => {
    const file = $('csvFile').files[0];
    $('csvErr').textContent = ''; $('csvResult').textContent = '';
    if (!file) { $('csvErr').textContent = 'Choose a CSV file first.'; return; }
    const body = new FormData(); body.append('file', file);
    $('csvPreview').disabled = $('csvUpload').disabled = true;
    try {
      const response = await fetch(`/api/admin/products/import${preview ? '?preview=1' : ''}`, { method: 'POST', body, credentials: 'same-origin' });
      const data = await response.json().catch(() => null);
      if (response.status === 401) { showLogin(); return; }
      if (!response.ok) throw new Error((data && data.error) || `Upload failed (${response.status})`);
      const lines = [
        `${preview ? 'Preview: would' : 'Done:'} ${preview ? 'add' : 'added'} ${data.added} and ${preview ? 'update' : 'updated'} ${data.updated} product(s) from ${data.rows} row(s).`,
        ...(data.database ? [`Database now has ${data.database.products} products, ${data.database.sellable} sellable, ${data.database.barcodes} barcodes.`] : []),
        ...(data.problems.length ? ['Notes:', ...data.problems.map((p) => `• ${p}`)] : []),
      ];
      $('csvResult').innerHTML = lines.map((l) => `<div>${esc(l)}</div>`).join('');
      if (!preview) { toast('Products imported'); $('csvFile').value = ''; }
    } catch (err) { $('csvErr').textContent = err.message; }
    finally { $('csvPreview').disabled = $('csvUpload').disabled = false; }
  };
  $('csvPreview').addEventListener('click', () => sendCsv(true));
  $('csvForm').addEventListener('submit', (e) => { e.preventDefault(); sendCsv(false); });

  document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeSheets));
  $('scrim').addEventListener('click', closeSheets);

  const boot = async () => {
    const config = await api('/api/config');
    if (!config.configured && !config.hasUsers) $('loginHint').textContent = 'No master code is configured yet. Create the POS_ACCESS_CODE secret, then reload.';
    else if (!config.hasUsers) { setMode('master'); $('loginHint').textContent = 'No users yet. Sign in with the master code to add the first admin.'; }
    if (config.unlocked && config.role === 'admin') { me = { staff: config.staff, role: 'admin' }; await showAdmin(); }
    else showLogin();
  };
  boot().catch((err) => { $('loginErr').textContent = err.message; showLogin(); });
})();
