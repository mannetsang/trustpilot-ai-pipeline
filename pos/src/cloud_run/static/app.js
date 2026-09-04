/* Superhairpieces POS register. Plain JS, no build step.

   The browser holds the cart; the server prices every line from the database
   and checks the totals again before writing a sale. Each sale carries a
   clientRecordId generated here, so a retried send after a dropped connection
   can never record the same sale twice. */

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // The payment screen (cash received, quick amounts, keypad, change) is
  // switched off for now: Charge records the sale at the exact total straight
  // away. Set this to true to bring the screen back; its code is all still here.
  const PAYMENT_SCREEN = false;

  const state = {
    config: null,
    category: 'all',
    products: [],
    cart: [],                     // [{ product, qty, barcode }]
    discount: 0,
    cashCents: 0,
    pendingSale: null,            // the payload being sent, kept for retry
    orders: [],
    selectedOrder: null,
  };

  // ------------------------------------------------------------ utilities

  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch { /* no body */ }
    if (response.status === 401) { showLock(); throw new Error('Till is locked'); }
    if (!response.ok) throw new Error((data && data.error) || `Request failed (${response.status})`);
    return data;
  };

  const money = (value) => {
    const n = Number(value || 0);
    const cur = state.config?.currency || 'CAD';
    try {
      return new Intl.NumberFormat('en-CA', { style: 'currency', currency: cur }).format(n);
    } catch { return `${cur} ${n.toFixed(2)}`; }
  };
  const cents = (n) => Math.round(n * 100) / 100;
  const initials = (name) => name.trim().split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase() || '?';

  let toastTimer;
  const toast = (msg) => {
    const el = $('toast');
    el.textContent = msg;
    el.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), 2600);
  };

  const recordId = () => (crypto.randomUUID ? crypto.randomUUID() : `r${Date.now()}${Math.random().toString(36).slice(2)}`);

  // ------------------------------------------------------------ views

  const go = (view) => {
    document.querySelectorAll('.view').forEach((v) => v.classList.toggle('on', v.id === `view-${view}`));
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('on', b.dataset.view === view || (view === 'pay' && b.dataset.view === 'sell') || (view === 'done' && b.dataset.view === 'sell')));
    $('pageTitle').textContent = { sell: 'Sell', pay: 'Payment', done: 'Sale complete', orders: 'Sales', analytics: 'Analytics' }[view] || 'Sell';
    if (view === 'sell') setTimeout(() => $('searchInput').focus(), 50);
    if (view === 'orders') loadOrders();
    if (view === 'analytics') loadAnalytics();
  };

  const openSheet = (id) => { $('scrim').classList.add('on'); $(id).classList.add('on'); };
  const closeSheets = () => { $('scrim').classList.remove('on'); document.querySelectorAll('.sheet').forEach((s) => s.classList.remove('on')); };

  // ------------------------------------------------------------ lock screen

  const showLock = () => { $('lock').hidden = false; $('app').hidden = true; loadNames(); setTimeout(() => $('lockCode').focus(), 50); };
  const showApp = () => { $('lock').hidden = true; $('app').hidden = false; renderUser(); go('sell'); };

  $('lockForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('lockErr').textContent = '';
    $('lockBtn').disabled = true;
    try {
      const staff = $('lockStaff').value.trim();
      const data = await api('/api/unlock', { method: 'POST', body: { pin: $('lockCode').value, staff } });
      state.config.unlocked = true;
      state.config.staff = data.staff;
      state.config.role = data.role;
      localStorage.setItem('pos.staff', data.staff);
      $('lockCode').value = '';
      showApp();
      await loadCatalogue();
    } catch (err) {
      $('lockErr').textContent = err.message;
    } finally {
      $('lockBtn').disabled = false;
    }
  });

  $('lockOut').addEventListener('click', async () => {
    await api('/api/lock', { method: 'POST' }).catch(() => {});
    state.config.unlocked = false;
    showLock();
  });

  // ------------------------------------------------------------ user

  const renderUser = () => {
    $('navAnalytics').hidden = state.config?.role !== 'admin';
    const name = state.config?.staff || '';
    $('userName').textContent = name;
    $('userAv').textContent = initials(name);
    $('locPill').textContent = `${state.config.locationName} · ${state.config.currency}`;
  };
  const loadNames = async () => {
    try {
      const names = await api('/api/users');
      $('lockNames').innerHTML = names.map((u) => `<button type="button" class="chip" data-name="${esc(u.name)}">${esc(u.name)}</button>`).join('');
      if (!names.length) $('lockHint').textContent = 'No users yet. Open the admin portal with the master code to add the first one.';
    } catch { /* names are a convenience; typing still works */ }
  };
  $('lockNames').addEventListener('click', (e) => {
    const b = e.target.closest('[data-name]');
    if (!b) return;
    $('lockStaff').value = b.dataset.name;
    document.querySelectorAll('#lockNames .chip').forEach((c) => c.classList.toggle('on', c === b));
    $('lockCode').focus();
  });

  // ------------------------------------------------------------ catalogue

  const loadCatalogue = async () => {
    const cats = await api('/api/categories');
    $('catChips').innerHTML = [{ category: 'all', n: cats.reduce((a, c) => a + c.n, 0) }, ...cats]
      .map((c) => `<button class="chip${state.category === c.category ? ' on' : ''}" data-cat="${esc(c.category)}">${c.category === 'all' ? 'All' : esc(c.category)} <span style="opacity:.6">${c.n}</span></button>`)
      .join('');
    await searchProducts();
  };

  let searchTimer;
  const searchProducts = async () => {
    const q = $('searchInput').value.trim();
    const params = new URLSearchParams({ q, category: state.category });
    state.products = await api(`/api/products?${params}`);
    renderGrid();
  };

  const renderGrid = () => {
    if (!state.products.length) { $('grid').innerHTML = '<div class="empty">Nothing matches.</div>'; return; }
    $('grid').innerHTML = state.products.map((p) => `
      <button class="card" data-id="${esc(p.id)}">
        ${thumb(p, 'photo')}
        <div class="name">${esc(p.name)}</div>
        <div class="meta"><span class="tag">${esc(p.category)}</span>${p.isSet ? '<span class="tag">set</span>' : ''}${p.isClearance ? '<span class="tag warn">clearance</span>' : ''}</div>
        <div class="price money">${money(p.price)}</div>
      </button>`).join('');
  };

  // Product photo, or a neutral placeholder with the first letters of the name.
  // A photo that fails to load falls back to the placeholder (onerror).
  const thumb = (p, cls) => {
    const ph = `<div class="${cls} ph" aria-hidden="true">${esc(p.name.replace(/^[A-Za-z]+_/, '').slice(0, 2).toUpperCase())}</div>`;
    if (!p.image) return ph;
    return `<img class="${cls}" src="${esc(p.image)}" alt="" loading="lazy" referrerpolicy="no-referrer"
      onerror="this.outerHTML=${esc(JSON.stringify(ph))}">`;
  };

  $('catChips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-cat]');
    if (!b) return;
    state.category = b.dataset.cat;
    document.querySelectorAll('#catChips .chip').forEach((c) => c.classList.toggle('on', c.dataset.cat === state.category));
    searchProducts().catch((err) => toast(err.message));
  });

  $('grid').addEventListener('click', (e) => {
    const b = e.target.closest('[data-id]');
    if (!b) return;
    const p = state.products.find((x) => x.id === b.dataset.id);
    if (p) addToCart(p);
  });

  // Typing searches; a scanner sends the code followed by Enter, which we treat
  // as a barcode lookup first and a plain search second.
  $('searchInput').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchProducts().catch((err) => toast(err.message)), 180);
  });
  $('searchInput').addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const code = $('searchInput').value.trim();
    if (!code) return;
    clearTimeout(searchTimer);
    await scan(code);
  });

  /** A scanned or typed code: barcode lookup first, then a name/SKU search. */
  const scan = async (code) => {
    try {
      const p = await api(`/api/barcode/${encodeURIComponent(code)}`);
      addToCart(p, code);
      toast(`Added ${p.name}`);
      $('searchInput').value = '';
      await searchProducts();
    } catch (err) {
      if (err.message === 'Till is locked') return;
      $('searchInput').value = code;
      await searchProducts();
      if (state.products.length === 1) {
        addToCart(state.products[0]);
        toast(`Added ${state.products[0].name}`);
        $('searchInput').value = '';
        await searchProducts();
      } else if (!state.products.length) {
        toast(`No product for "${code}"`);
      }
    }
  };

  // The scanner works without tapping the search box. A scanner is a keyboard
  // that types the whole code in a fast burst and ends with Enter, so on the
  // Sell screen any burst of characters arriving while no text field has focus
  // is collected and, on Enter, treated as a scan. A pause longer than 150 ms
  // starts a new code, so stray single keys never combine into one.
  let scanBuffer = '';
  let scanLastKey = 0;
  document.addEventListener('keydown', (e) => {
    if ($('app').hidden || !$('view-sell').classList.contains('on')) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const now = Date.now();
    if (now - scanLastKey > 150) scanBuffer = '';
    scanLastKey = now;
    if (e.key === 'Enter') {
      const code = scanBuffer.trim();
      scanBuffer = '';
      if (code.length >= 3) { e.preventDefault(); scan(code); }
      return;
    }
    if (e.key.length === 1) { scanBuffer += e.key; e.preventDefault(); }
  });

  // ------------------------------------------------------------ cart

  const addToCart = (product, barcode) => {
    const line = state.cart.find((l) => l.product.id === product.id);
    if (line) line.qty += 1;
    else state.cart.push({ product, qty: 1, barcode: barcode || null });
    renderCart();
  };

  const cartTotals = () => {
    const subtotal = cents(state.cart.reduce((a, l) => a + Number(l.product.price) * l.qty, 0));
    const discount = Math.min(cents(Math.max(0, state.discount || 0)), subtotal);
    const total = cents(subtotal - discount);
    return { subtotal, discount, total };
  };

  const totalsHtml = (t) => `
    <div class="trow"><span>Subtotal</span><span class="money">${money(t.subtotal)}</span></div>
    ${t.discount ? `<div class="trow"><span>Discount</span><span class="money">−${money(t.discount)}</span></div>` : ''}
    <div class="trow grand"><span>Total</span><span class="money">${money(t.total)}</span></div>`;

  const renderCart = () => {
    if (!PAYMENT_SCREEN) state.pendingSale = null;   // the cart changed; a retry must send the new cart
    const count = state.cart.reduce((a, l) => a + l.qty, 0);
    $('cartCount').textContent = `${count} item${count === 1 ? '' : 's'}`;
    $('cartLines').innerHTML = state.cart.length ? state.cart.map((l) => `
      <div class="line" data-id="${esc(l.product.id)}">
        <div class="line-main">${thumb(l.product, 'mini')}<div><div class="n">${esc(l.product.name)}</div><div class="s">${esc(l.product.category)} · ${money(l.product.price)} each</div></div></div>
        <div class="amt money">${money(Number(l.product.price) * l.qty)}</div>
        <div class="qty">
          <button data-step="-1" aria-label="Less">−</button><span>${l.qty}</span><button data-step="1" aria-label="More">+</button>
          <button class="rm" data-remove>Remove</button>
        </div>
      </div>`).join('') : '<div class="empty">Tap a product or scan a barcode.</div>';
    const t = cartTotals();
    $('cartTotals').innerHTML = totalsHtml(t);
    $('chargeBtn').disabled = !state.cart.length;
    $('chargeBtn').textContent = state.cart.length ? `Charge ${money(t.total)}` : 'Charge';
  };

  $('cartLines').addEventListener('click', (e) => {
    const lineEl = e.target.closest('.line');
    if (!lineEl) return;
    const line = state.cart.find((l) => l.product.id === lineEl.dataset.id);
    if (!line) return;
    if (e.target.closest('[data-remove]')) state.cart = state.cart.filter((l) => l !== line);
    else if (e.target.closest('[data-step]')) {
      line.qty += Number(e.target.closest('[data-step]').dataset.step);
      if (line.qty <= 0) state.cart = state.cart.filter((l) => l !== line);
    }
    renderCart();
  });
  $('clearBtn').addEventListener('click', () => { state.cart = []; state.discount = 0; state.pendingSale = null; $('discountInput').value = ''; renderCart(); });
  $('discountInput').addEventListener('input', () => { state.discount = Number($('discountInput').value) || 0; renderCart(); });

  // ------------------------------------------------------------ payment

  $('chargeBtn').addEventListener('click', () => (PAYMENT_SCREEN ? openPayment() : chargeExact()));

  /** Record the sale at the exact total, no payment screen. Re-pressing after a
      failure resends the same payload, so a retry can never double-record. */
  const chargeExact = async () => {
    if (!state.cart.length) return;
    const payload = state.pendingSale || {
      clientRecordId: recordId(),
      items: state.cart.map((l) => ({ productId: l.product.id, quantity: l.qty, barcode: l.barcode })),
      discount: cartTotals().discount,
      cashReceived: cartTotals().total,
    };
    state.pendingSale = payload;
    const btn = $('chargeBtn');
    btn.disabled = true;
    btn.textContent = 'Recording sale…';
    try {
      const sale = await api('/api/sales', { method: 'POST', body: payload });
      state.pendingSale = null;
      finishSale(sale);
    } catch (err) {
      toast(`${err.message}. Check the connection and tap Charge again.`);
      btn.disabled = false;
      btn.textContent = 'Retry charge';
    }
  };
  $('payBack').addEventListener('click', () => go('sell'));

  const openPayment = () => {
    const t = cartTotals();
    state.cashCents = 0;
    $('payAmount').textContent = money(t.total);
    $('payLines').innerHTML = state.cart.map((l) => `<div class="ps-line"><span>${l.qty}× ${esc(l.product.name)}</span><span class="money">${money(Number(l.product.price) * l.qty)}</span></div>`).join('');
    $('payTotals').innerHTML = totalsHtml(t);
    const amounts = [t.total, Math.ceil(t.total / 5) * 5, Math.ceil(t.total / 20) * 20, Math.ceil(t.total / 50) * 50, Math.ceil(t.total / 100) * 100]
      .filter((v, i, a) => v > 0 && a.indexOf(v) === i).slice(0, 4);
    $('cashQuick').innerHTML = amounts.map((v, i) => `<button class="qbtn money" data-cash="${Math.round(v * 100)}">${i === 0 ? 'Exact ' : ''}${money(v)}</button>`).join('');
    $('payErr').textContent = '';
    renderCash();
    go('pay');
  };

  $('cashKeys').innerHTML = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '00', '0', '⌫'].map((k) => `<button class="key" data-key="${k}">${k}</button>`).join('');
  $('cashKeys').addEventListener('click', (e) => {
    const k = e.target.closest('[data-key]')?.dataset.key;
    if (!k) return;
    if (k === '⌫') state.cashCents = Math.floor(state.cashCents / 10);
    else if (k === '00') state.cashCents *= 100;
    else state.cashCents = state.cashCents * 10 + Number(k);
    if (state.cashCents > 99999999) state.cashCents = 99999999;
    renderCash();
  });
  $('cashQuick').addEventListener('click', (e) => {
    const b = e.target.closest('[data-cash]');
    if (!b) return;
    state.cashCents = Number(b.dataset.cash);
    renderCash();
  });

  const renderCash = () => {
    const t = cartTotals();
    const cash = state.cashCents / 100;
    $('cashVal').textContent = money(cash);
    const ok = cash >= t.total - 1e-9;
    $('cashTender').disabled = !ok || !!state.pendingSale;
    $('cashTender').textContent = ok ? `Tender cash · change ${money(cents(cash - t.total))}` : 'Tender cash';
  };

  $('cashTender').addEventListener('click', async () => {
    if (state.pendingSale) return;
    const t = cartTotals();
    const payload = {
      clientRecordId: recordId(),
      items: state.cart.map((l) => ({ productId: l.product.id, quantity: l.qty, barcode: l.barcode })),
      discount: t.discount,
      cashReceived: state.cashCents / 100,
    };
    await sendSale(payload);
  });

  const sendSale = async (payload) => {
    state.pendingSale = payload;
    $('cashTender').disabled = true;
    $('cashTender').textContent = 'Recording sale…';
    $('payErr').textContent = '';
    try {
      const sale = await api('/api/sales', { method: 'POST', body: payload });
      state.pendingSale = null;
      finishSale(sale);
    } catch (err) {
      // Keep the same clientRecordId: the retry cannot double-record.
      $('payErr').textContent = `${err.message}. Check the connection and tap again to retry.`;
      $('cashTender').disabled = false;
      $('cashTender').textContent = 'Retry tender';
      $('cashTender').onclick = () => sendSale(payload);
    }
  };

  const finishSale = (sale) => {
    $('cashTender').onclick = null;
    const change = Number(sale.change);
    $('doneKicker').textContent = change > 0.004 ? 'Change due' : 'Sale recorded · cash';
    $('doneAmount').textContent = money(change > 0.004 ? change : sale.total);
    $('doneMeta').textContent = `Sale #${sale.number} · ${money(sale.total)} · ${sale.staff} · ${state.config.locationName}`;
    state.cart = [];
    state.discount = 0;
    $('discountInput').value = '';
    renderCart();
    go('done');
  };
  $('newSaleBtn').addEventListener('click', () => go('sell'));

  // ------------------------------------------------------------ orders

  const loadOrders = async () => {
    try {
      const q = $('orderSearch').value.trim();
      const [orders, summary] = await Promise.all([
        api(`/api/sales?${new URLSearchParams({ q, limit: 100 })}`),
        api('/api/summary'),
      ]);
      state.orders = orders;
      renderOrders();
      renderSummary(summary);
    } catch (err) { toast(err.message); }
  };

  const statusOf = (o) => (o.status === 'refunded' ? ['refunded', 'Refunded'] : o.refundAmount ? ['partial', 'Part refunded'] : ['completed', 'Paid']);

  const renderOrders = () => {
    $('orderList').innerHTML = state.orders.length ? state.orders.map((o) => {
      const [cls, label] = statusOf(o);
      const when = new Date(o.soldAt).toLocaleTimeString('en-CA', { hour: 'numeric', minute: '2-digit' });
      return `<button class="order${state.selectedOrder === o.number ? ' on' : ''}" data-num="${o.number}">
        <div class="num">#${o.number}</div>
        <div><div>${o.items.length} line${o.items.length === 1 ? '' : 's'} · ${esc(o.staff)}</div><div class="who">${when} · <span class="status ${cls}">${label}</span></div></div>
        <div class="tot money">${money(o.total)}</div>
      </button>`;
    }).join('') : '<div class="empty">No sales yet.</div>';
  };

  const renderSummary = (rows) => {
    if (!rows.length) { $('summaryTable').innerHTML = ''; return; }
    $('summaryTable').innerHTML = `<tr><th>Day</th><th>Sales</th><th>Taken</th><th>Cash refunded</th><th>Net in drawer</th></tr>`
      + rows.map((r) => `<tr><td>${esc(r.day)}</td><td>${r.sales}</td><td class="money">${money(r.amount_paid)}</td><td class="money">${money(r.cash_refunded)}</td><td class="money"><b>${money(r.net_cash_in_drawer)}</b></td></tr>`).join('');
  };

  $('orderList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-num]');
    if (!b) return;
    state.selectedOrder = Number(b.dataset.num);
    renderOrders();
    renderDetail();
  });
  $('refreshOrders').addEventListener('click', loadOrders);
  let orderTimer;
  $('orderSearch').addEventListener('input', () => { clearTimeout(orderTimer); orderTimer = setTimeout(loadOrders, 250); });

  const renderDetail = () => {
    const o = state.orders.find((x) => x.number === state.selectedOrder);
    if (!o) { $('orderDetail').innerHTML = '<div class="muted">Pick a sale to see its lines.</div>'; return; }
    const [, label] = statusOf(o);
    $('orderDetail').innerHTML = `
      <h3>Sale #${o.number}</h3>
      <div class="muted">${new Date(o.soldAt).toLocaleString('en-CA')} · ${esc(o.staff)} · ${label}</div>
      <div>${o.items.map((i) => `<div class="ps-line"><span>${i.quantity}× ${esc(i.name)}</span><span class="money">${money(i.lineTotal)}</span></div>`).join('')}</div>
      <div class="totals" style="padding:0">
        <div class="trow"><span>Subtotal</span><span class="money">${money(o.subtotal)}</span></div>
        ${Number(o.discount) ? `<div class="trow"><span>Discount</span><span class="money">−${money(o.discount)}</span></div>` : ''}
        <div class="trow grand"><span>Total</span><span class="money">${money(o.total)}</span></div>
        <div class="trow"><span>Cash received</span><span class="money">${money(o.cashReceived)}</span></div>
        <div class="trow"><span>Change</span><span class="money">${money(o.change)}</span></div>
        ${o.refundAmount ? `<div class="trow" style="color:var(--danger)"><span>Refunded · ${esc(o.refundReason || '')}</span><span class="money">−${money(o.refundAmount)}</span></div>` : ''}
      </div>
      ${o.refundAmount ? '' : `<button class="btn danger" id="refundBtn">Refund</button>`}`;
    const rb = $('refundBtn');
    if (rb) rb.addEventListener('click', () => {
      $('refundNum').textContent = `#${o.number}`;
      $('refundReason').value = ''; $('refundAmount').value = ''; $('refundCode').value = ''; $('refundAdminName').value = ''; $('refundAdminPin').value = ''; $('refundErr').textContent = '';
      $('refundApproval').hidden = state.config.role === 'admin';
      $('refundAmount').placeholder = `Amount (blank = full ${money(o.amountPaid)})`;
      openSheet('refundSheet');
    });
  };

  $('refundConfirm').addEventListener('click', async () => {
    const o = state.orders.find((x) => x.number === state.selectedOrder);
    if (!o) return;
    const body = { reason: $('refundReason').value, code: $('refundCode').value, adminName: $('refundAdminName').value, adminPin: $('refundAdminPin').value };
    if ($('refundAmount').value) body.amount = Number($('refundAmount').value);
    if (!body.reason) { $('refundErr').textContent = 'Pick a reason'; return; }
    try {
      await api(`/api/sales/${o.number}/refund`, { method: 'POST', body });
      closeSheets();
      toast(`Refunded sale #${o.number}`);
      await loadOrders();
      renderDetail();
    } catch (err) { $('refundErr').textContent = err.message; }
  });

  // ------------------------------------------------------------ analytics (admins)

  const an = { range: 'all', from: '', to: '', data: null };
  const num = (v) => Number(v || 0);
  const money0 = (v) => { const n = num(v); return Math.abs(n) >= 10000 ? money(n).replace(/\.\d\d$/, '') : money(n); };
  const pct = (v) => (v == null ? '—' : `${Number(v).toFixed(v >= 10 ? 0 : 1)}%`);
  const localDate = (d) => { const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000); return z.toISOString().slice(0, 10); };

  const rangeDates = () => {
    const today = new Date();
    const day = (offset) => { const d = new Date(today); d.setDate(d.getDate() + offset); return localDate(d); };
    switch (an.range) {
      case 'today': return [day(0), day(0)];
      case 'yesterday': return [day(-1), day(-1)];
      case '7d': return [day(-6), day(0)];
      case 'custom': return [an.from, an.to];
      default: return ['', ''];
    }
  };

  const loadAnalytics = async () => {
    const [from, to] = rangeDates();
    const params = new URLSearchParams(); if (from) params.set('from', from); if (to) params.set('to', to);
    $('anExport').href = `/api/analytics/export.csv${params.toString() ? '?' + params : ''}`;
    try {
      an.data = await api(`/api/analytics?${params}`);
      renderAnalytics();
    } catch (err) { toast(err.message); }
  };

  $('anRanges').addEventListener('click', (e) => {
    const b = e.target.closest('[data-range]'); if (!b) return;
    an.range = b.dataset.range; $('anFrom').value = ''; $('anTo').value = '';
    document.querySelectorAll('#anRanges .chip').forEach((c) => c.classList.toggle('on', c === b));
    loadAnalytics();
  });
  $('anApply').addEventListener('click', () => {
    an.from = $('anFrom').value; an.to = $('anTo').value;
    if (!an.from && !an.to) { toast('Pick a from or to date'); return; }
    an.range = 'custom';
    document.querySelectorAll('#anRanges .chip').forEach((c) => c.classList.remove('on'));
    loadAnalytics();
  });
  $('anRefresh').addEventListener('click', loadAnalytics);

  const kpiTile = (label, value, detail, hero) => `<div class="kpi"><div class="l">${esc(label)}</div><div class="v${hero ? ' hero' : ''} money">${esc(value)}</div>${detail ? `<div class="d">${esc(detail)}</div>` : ''}</div>`;

  const renderAnalytics = () => {
    const d = an.data; const k = d.kpi;
    const r = d.range;
    $('anRangeNote').textContent = (r.from || r.to)
      ? `Showing ${r.from || 'the start'} to ${r.to || 'today'} · ${k.sales} sales`
      : `Showing everything recorded · ${k.sales} sales${k.firstSale ? ` since ${new Date(k.firstSale).toLocaleDateString('en-CA')}` : ''}`;
    const coverage = k.costCoveragePct == null ? '' : `cost known for ${pct(k.costCoveragePct)} of revenue`;
    $('anKpis').innerHTML = [
      kpiTile('Net revenue', money0(k.netRevenue), `${money0(k.grossRevenue)} sold, ${money0(k.refunds)} refunded`, true),
      kpiTile('Gross margin', k.grossMargin == null ? '—' : money0(k.grossMargin), k.grossMarginPct == null ? 'no product costs yet' : `${pct(k.grossMarginPct)} · ${coverage}`),
      kpiTile('Sales', String(k.sales), `${k.units} units · ${k.refundedSales} refunded`),
      kpiTile('Average sale', k.avgBasket == null ? '—' : money(k.avgBasket), k.sales ? `${(k.units / k.sales).toFixed(1)} units per sale` : ''),
      kpiTile('Discounts given', money0(k.discounts), k.grossRevenue > 0 ? `${pct(num(k.discounts) / (num(k.grossRevenue) + num(k.discounts)) * 100)} of list` : ''),
    ].join('');

    barChart($('figDay'), d.byDay.map((x) => ({ label: x.day.slice(5), value: num(x.revenue) - num(x.refunds), tip: `${x.day}: ${money(num(x.revenue) - num(x.refunds))} net · ${x.sales} sales · ${x.units} units` })), { columns: true });
    const hours = Array.from({ length: 24 }, (_, h) => ({ h, sales: 0, revenue: 0 }));
    d.byHour.forEach((x) => { hours[x.hour] = { h: x.hour, sales: x.sales, revenue: num(x.revenue) }; });
    const active = hours.filter((x, i, a) => x.sales || (a.slice(0, i).some((y) => y.sales) && a.slice(i).some((y) => y.sales)));
    barChart($('figHour'), active.map((x) => ({ label: `${x.h}h`, value: x.revenue, tip: `${x.h}:00–${x.h}:59: ${money(x.revenue)} · ${x.sales} sales` })), { columns: true, labelEvery: active.length > 12 ? 2 : 1 });
    barChart($('figCategory'), d.byCategory.map((x) => ({ label: x.category, value: num(x.revenue), tip: `${x.category}: ${money(num(x.revenue))} · ${x.units} units${x.cost_of_goods != null ? ` · margin ${pct((num(x.revenue_with_cost) - num(x.cost_of_goods)) / num(x.revenue_with_cost) * 100)}` : ''}` })));
    barChart($('figStaff'), d.byStaff.map((x) => ({ label: x.staff, value: num(x.revenue), tip: `${x.staff}: ${money(num(x.revenue))} · ${x.sales} sales · ${money(num(x.refunds))} refunded` })));
    stackChart($('figStock'), d.stock.filter((x) => num(x.brought) > 0).map((x) => ({ label: x.category, sold: num(x.sold), total: num(x.brought), tip: `${x.category}: ${x.sold} of ${x.brought} units sold (${pct(num(x.sold) / num(x.brought) * 100)}) · ${money0(x.remaining_value)} of stock left at price` })));
    barChart($('figTop'), d.topProducts.slice(0, 12).map((x) => ({ label: x.name || x.sku, value: num(x.revenue), tip: `${x.name || x.sku}: ${money(num(x.revenue))} · ${x.units} units${x.margin != null ? ` · margin ${money(num(x.margin))}` : ''}${x.brought ? ` · ${x.units}/${x.brought} brought` : ''}` })), { labelWidth: 220 });

    $('tblDay').innerHTML = table(['Day', 'Sales', 'Units', 'Sold', 'Refunds', 'Net'], d.byDay.map((x) => [x.day, x.sales, x.units, money(num(x.revenue)), money(num(x.refunds)), money(num(x.revenue) - num(x.refunds))]));
    $('tblCategory').innerHTML = table(['Category', 'Units', 'Revenue', 'Cost', 'Margin'], d.byCategory.map((x) => [x.category, x.units, money(num(x.revenue)), x.cost_of_goods == null ? '—' : money(num(x.cost_of_goods)), x.cost_of_goods == null ? '—' : pct((num(x.revenue_with_cost) - num(x.cost_of_goods)) / num(x.revenue_with_cost) * 100)]));
  };

  const table = (head, rows) => rows.length
    ? `<table><tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr>${rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table>`
    : '<div class="hint">No sales in this range.</div>';

  const attachTips = (fig) => {
    const plot = fig.querySelector('.plot');
    let tip = plot.querySelector('.tip');
    if (!tip) { tip = document.createElement('div'); tip.className = 'tip'; plot.appendChild(tip); }
    plot.querySelectorAll('[data-tip]').forEach((el) => {
      el.addEventListener('mousemove', (e) => { const r = plot.getBoundingClientRect(); tip.textContent = el.dataset.tip; tip.style.left = `${e.clientX - r.left}px`; tip.style.top = `${e.clientY - r.top}px`; tip.classList.add('on'); });
      el.addEventListener('mouseleave', () => tip.classList.remove('on'));
    });
  };

  const nice = (max) => { if (max <= 0) return 1; const p = 10 ** Math.floor(Math.log10(max)); const m = max / p; const step = m <= 1 ? 0.25 : m <= 2 ? 0.5 : m <= 5 ? 1 : 2; return Math.ceil(m / step) * step * p; };

  /** Horizontal bars (default) or columns; single series, sequential hue; hover tooltips; end labels. */
  const barChart = (fig, items, opts = {}) => {
    const plot = fig.querySelector('.plot');
    if (!items.length) { plot.innerHTML = '<div class="empty hint">No sales in this range.</div>'; return; }
    const W = 560; const max = nice(Math.max(...items.map((i) => i.value)));
    let svg = '';
    if (opts.columns) {
      const H = 220, padL = 44, padB = 26, padT = 14; const w = (W - padL) / items.length; const bw = Math.min(24, w * 0.6);
      const y = (v) => padT + (H - padT - padB) * (1 - v / max);
      for (let g = 0; g <= 4; g++) { const v = max * g / 4; svg += `<line class="grid" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${padL - 6}" y="${y(v) + 3}" text-anchor="end">${money0(v)}</text>`; }
      items.forEach((it, i) => {
        const x = padL + w * i + (w - bw) / 2; const top = y(it.value); const h = Math.max(0, y(0) - top);
        svg += `<rect class="hit" data-tip="${esc(it.tip)}" x="${padL + w * i}" y="${padT}" width="${w}" height="${H - padT - padB}"/>`;
        svg += `<path class="bar" data-tip="${esc(it.tip)}" d="${roundTop(x, top, bw, h)}"/>`;
        if (!opts.labelEvery || i % opts.labelEvery === 0) svg += `<text x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${esc(it.label)}</text>`;
        if (items.length <= 8 && it.value > 0) svg += `<text class="val" x="${x + bw / 2}" y="${top - 4}" text-anchor="middle">${money0(it.value)}</text>`;
      });
      svg += `<line class="axis" x1="${padL}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>`;
      plot.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${svg}</svg>`;
    } else {
      const lw = opts.labelWidth || 110, row = 30, padT = 6; const H = padT + row * items.length + 6; const x0 = lw + 8, x1 = W - 70;
      const x = (v) => x0 + (x1 - x0) * v / max;
      items.forEach((it, i) => {
        const yy = padT + row * i + 5; const bw = x(it.value) - x0;
        svg += `<rect class="hit" data-tip="${esc(it.tip)}" x="0" y="${padT + row * i}" width="${W}" height="${row}"/>`;
        svg += `<text class="cat" x="${lw}" y="${yy + 14}" text-anchor="end">${esc(truncate(it.label, lw / 6.2))}</text>`;
        svg += `<path class="bar" data-tip="${esc(it.tip)}" d="${roundRight(x0, yy, bw, 20)}"/>`;
        svg += `<text class="val" x="${x0 + bw + 6}" y="${yy + 14}">${money0(it.value)}</text>`;
      });
      svg += `<line class="axis" x1="${x0}" x2="${x0}" y1="${padT}" y2="${H - 6}"/>`;
      plot.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${svg}</svg>`;
    }
    attachTips(fig);
  };

  /** Sold (accent) against brought (context), one row per category, with a legend and percent label. */
  const stackChart = (fig, items) => {
    const plot = fig.querySelector('.plot');
    if (!items.length) { plot.innerHTML = '<div class="empty hint">No stock quantities on the product list.</div>'; return; }
    const W = 560, lw = 110, row = 30, padT = 6; const H = padT + row * items.length + 6; const x0 = lw + 8, x1 = W - 60;
    const max = Math.max(...items.map((i) => i.total)); const x = (v) => x0 + (x1 - x0) * v / max;
    let svg = '';
    items.forEach((it, i) => {
      const yy = padT + row * i + 5;
      svg += `<rect class="hit" data-tip="${esc(it.tip)}" x="0" y="${padT + row * i}" width="${W}" height="${row}"/>`;
      svg += `<text class="cat" x="${lw}" y="${yy + 14}" text-anchor="end">${esc(truncate(it.label, 17))}</text>`;
      svg += `<path class="bar ctx" data-tip="${esc(it.tip)}" d="${roundRight(x0, yy, x(it.total) - x0, 20)}"/>`;
      if (it.sold > 0) svg += `<rect class="bar" data-tip="${esc(it.tip)}" x="${x0}" y="${yy}" width="${Math.max(0, x(Math.min(it.sold, it.total)) - x0 - 2)}" height="20"/>`;
      svg += `<text class="val" x="${x(it.total) + 6}" y="${yy + 14}">${pct(it.sold / it.total * 100)}</text>`;
    });
    plot.innerHTML = `<div class="legend"><span><i style="background:var(--series-1)"></i>Sold</span><span><i style="background:var(--context)"></i>Still on the shelf</span></div><svg viewBox="0 0 ${W} ${H}">${svg}</svg>`;
    attachTips(fig);
  };

  const roundTop = (x, y, w, h, r = 4) => h <= 0 ? '' : `M${x},${y + h} V${y + Math.min(r, h)} Q${x},${y} ${x + Math.min(r, w / 2)},${y} H${x + w - Math.min(r, w / 2)} Q${x + w},${y} ${x + w},${y + Math.min(r, h)} V${y + h} Z`;
  const roundRight = (x, y, w, h, r = 4) => w <= 0 ? '' : `M${x},${y} H${x + w - Math.min(r, w)} Q${x + w},${y} ${x + w},${y + r} V${y + h - r} Q${x + w},${y + h} ${x + w - Math.min(r, w)},${y + h} H${x} Z`;
  const truncate = (t, n) => (t.length > n ? t.slice(0, n - 1) + '…' : t);

  // ------------------------------------------------------------ shell

  document.querySelectorAll('.nav-btn').forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeSheets));
  $('scrim').addEventListener('click', closeSheets);

  const renderConnection = () => {
    $('connPill').classList.toggle('offline', !navigator.onLine);
    $('connText').textContent = navigator.onLine ? 'Online' : 'Offline';
  };
  window.addEventListener('online', renderConnection);
  window.addEventListener('offline', renderConnection);

  const boot = async () => {
    renderConnection();
    state.config = await api('/api/config');
    $('lockStaff').value = localStorage.getItem('pos.staff') || '';
    await loadNames();
    if (state.config.unlocked) { showApp(); await loadCatalogue(); }
    else showLock();
  };
  boot().catch((err) => { $('lockErr').textContent = err.message; showLock(); });
})();
