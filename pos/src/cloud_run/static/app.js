/* Superhairpieces POS register. Plain JS, no build step.

   The browser holds the cart; the server prices every line from the database
   and checks the totals again before writing a sale. Each sale carries a
   clientRecordId generated here, so a retried send after a dropped connection
   can never record the same sale twice. */

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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
    $('pageTitle').textContent = { sell: 'Sell', pay: 'Payment', done: 'Sale complete', orders: 'Sales' }[view] || 'Sell';
    if (view === 'sell') setTimeout(() => $('searchInput').focus(), 50);
    if (view === 'orders') loadOrders();
  };

  const openSheet = (id) => { $('scrim').classList.add('on'); $(id).classList.add('on'); };
  const closeSheets = () => { $('scrim').classList.remove('on'); document.querySelectorAll('.sheet').forEach((s) => s.classList.remove('on')); };

  // ------------------------------------------------------------ lock screen

  const showLock = () => { $('lock').hidden = false; $('app').hidden = true; setTimeout(() => $('lockCode').focus(), 50); };
  const showApp = () => { $('lock').hidden = true; $('app').hidden = false; renderUser(); go('sell'); };

  $('lockForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('lockErr').textContent = '';
    $('lockBtn').disabled = true;
    try {
      const staff = $('lockStaff').value.trim();
      const data = await api('/api/unlock', { method: 'POST', body: { code: $('lockCode').value, staff } });
      state.config.unlocked = true;
      state.config.staff = data.staff;
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
    const name = state.config?.staff || '';
    $('userName').textContent = name;
    $('userAv').textContent = initials(name);
    $('locPill').textContent = `${state.config.locationName} · ${state.config.currency}`;
  };
  $('userBtn').addEventListener('click', () => { $('userInput').value = state.config.staff || ''; $('userErr').textContent = ''; openSheet('userSheet'); $('userInput').focus(); });
  $('userSave').addEventListener('click', async () => {
    try {
      const data = await api('/api/staff', { method: 'POST', body: { staff: $('userInput').value } });
      state.config.staff = data.staff;
      localStorage.setItem('pos.staff', data.staff);
      renderUser();
      closeSheets();
    } catch (err) { $('userErr').textContent = err.message; }
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
        <div class="name">${esc(p.name)}</div>
        <div class="meta"><span class="tag">${esc(p.category)}</span>${p.isSet ? '<span class="tag">set</span>' : ''}${p.isClearance ? '<span class="tag warn">clearance</span>' : ''}</div>
        <div class="price money">${money(p.price)}</div>
      </button>`).join('');
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
    try {
      const p = await api(`/api/barcode/${encodeURIComponent(code)}`);
      addToCart(p, code);
      $('searchInput').value = '';
      await searchProducts();
    } catch {
      await searchProducts();
      if (state.products.length === 1) { addToCart(state.products[0]); $('searchInput').value = ''; await searchProducts(); }
      else if (!state.products.length) toast(`No product for "${code}"`);
    }
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
    const rate = Number(state.config.taxRate);
    const tax = cents(total - total / (1 + rate));
    return { subtotal, discount, total, tax };
  };

  const totalsHtml = (t) => `
    <div class="trow"><span>Subtotal</span><span class="money">${money(t.subtotal)}</span></div>
    ${t.discount ? `<div class="trow"><span>Discount</span><span class="money">−${money(t.discount)}</span></div>` : ''}
    <div class="trow"><span>Includes ${esc(state.config.taxLabel)}</span><span class="money">${money(t.tax)}</span></div>
    <div class="trow grand"><span>Total</span><span class="money">${money(t.total)}</span></div>`;

  const renderCart = () => {
    const count = state.cart.reduce((a, l) => a + l.qty, 0);
    $('cartCount').textContent = `${count} item${count === 1 ? '' : 's'}`;
    $('cartLines').innerHTML = state.cart.length ? state.cart.map((l) => `
      <div class="line" data-id="${esc(l.product.id)}">
        <div><div class="n">${esc(l.product.name)}</div><div class="s">${esc(l.product.category)} · ${money(l.product.price)} each</div></div>
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
  $('clearBtn').addEventListener('click', () => { state.cart = []; state.discount = 0; $('discountInput').value = ''; renderCart(); });
  $('discountInput').addEventListener('input', () => { state.discount = Number($('discountInput').value) || 0; renderCart(); });

  // ------------------------------------------------------------ payment

  $('chargeBtn').addEventListener('click', () => openPayment());
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
    $('doneKicker').textContent = change > 0.004 ? 'Change due' : 'Paid in cash';
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
    $('summaryTable').innerHTML = `<tr><th>Day</th><th>Sales</th><th>Taken</th><th>Tax incl.</th><th>Cash refunded</th><th>Net in drawer</th></tr>`
      + rows.map((r) => `<tr><td>${esc(r.day)}</td><td>${r.sales}</td><td class="money">${money(r.amount_paid)}</td><td class="money">${money(r.tax_total)}</td><td class="money">${money(r.cash_refunded)}</td><td class="money"><b>${money(r.net_cash_in_drawer)}</b></td></tr>`).join('');
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
        <div class="trow"><span>Includes ${esc(o.taxLabel || '')}</span><span class="money">${money(o.tax)}</span></div>
        <div class="trow grand"><span>Total</span><span class="money">${money(o.total)}</span></div>
        <div class="trow"><span>Cash received</span><span class="money">${money(o.cashReceived)}</span></div>
        <div class="trow"><span>Change</span><span class="money">${money(o.change)}</span></div>
        ${o.refundAmount ? `<div class="trow" style="color:var(--danger)"><span>Refunded · ${esc(o.refundReason || '')}</span><span class="money">−${money(o.refundAmount)}</span></div>` : ''}
      </div>
      ${o.refundAmount ? '' : `<button class="btn danger" id="refundBtn">Refund</button>`}`;
    const rb = $('refundBtn');
    if (rb) rb.addEventListener('click', () => {
      $('refundNum').textContent = `#${o.number}`;
      $('refundReason').value = ''; $('refundAmount').value = ''; $('refundCode').value = ''; $('refundErr').textContent = '';
      $('refundAmount').placeholder = `Amount (blank = full ${money(o.amountPaid)})`;
      openSheet('refundSheet');
    });
  };

  $('refundConfirm').addEventListener('click', async () => {
    const o = state.orders.find((x) => x.number === state.selectedOrder);
    if (!o) return;
    const body = { code: $('refundCode').value, reason: $('refundReason').value };
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
    if (!state.config.configured) {
      $('lockHint').textContent = 'The till code has not been set up yet. Create the POS_ACCESS_CODE secret, then reload.';
    }
    if (state.config.unlocked) { showApp(); await loadCatalogue(); }
    else showLock();
  };
  boot().catch((err) => { $('lockErr').textContent = err.message; showLock(); });
})();
