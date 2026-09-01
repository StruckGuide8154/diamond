const formatGBP = n => new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' }).format(Number(n || 0));
const FALLBACK_IMAGE = '/assets/images/shop-still-life.svg';

let cart = [];
try { cart = JSON.parse(localStorage.getItem('diamond-cart') || '[]'); } catch (_) { cart = []; }
cart = Array.isArray(cart) ? cart.filter(i => i && typeof i.id === 'string' && Number(i.qty) > 0) : [];

/* Catalogue copy is entered in the admin dashboard, so everything that reaches
   the DOM as markup is escaped first. */
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function safeImg(img) {
  if (!img) return;
  img.onerror = () => {
    if (img.src !== location.origin + FALLBACK_IMAGE) { img.src = FALLBACK_IMAGE; }
    else { img.classList.add('image-failed'); img.onerror = null; }
  };
}

function productById(id) { return PRODUCTS.find(x => x.id === id); }

function saveCart() { localStorage.setItem('diamond-cart', JSON.stringify(cart)); }

function card(p) {
  const stockText = p.stock > 0 ? `${p.stock} in stock` : 'Out of stock';
  return `<article class="product-card" data-cat="${esc(p.category)}" data-id="${esc(p.id)}">
    ${p.tag ? `<span class="tag">${esc(p.tag)}</span>` : ''}
    <a href="/product?id=${encodeURIComponent(p.id)}">
      <div class="product-image"><img onerror="safeImg(this)" src="${esc(p.image || FALLBACK_IMAGE)}" alt="${esc(p.name)}"></div>
      <div class="product-meta"><div class="product-brand">${esc(p.brand)}</div><div class="product-title">${esc(p.name)}</div><div class="price">${formatGBP(p.price)}</div><small class="stock-label">${stockText}</small></div>
    </a>
    <button class="quick-add" ${p.stock === 0 ? 'disabled' : ''} data-add="${esc(p.id)}" aria-label="Add ${esc(p.name)} to bag">${p.stock === 0 ? '×' : '+'}</button>
  </article>`;
}

function addCart(id) {
  const p = productById(id);
  if (!p) return;
  const line = cart.find(i => i.id === id);
  const current = line ? line.qty : 0;
  if (current >= p.stock) {
    alert(p.stock === 0 ? 'This item is currently out of stock.' : `Only ${p.stock} available.`);
    return;
  }
  if (line) { line.qty += 1; } else { cart.push({ id, qty: 1 }); }
  saveCart();
  renderCart();
  document.querySelector('.cart-drawer')?.classList.add('open');
}

function removeFromCart(id) {
  cart = cart.filter(i => i.id !== id);
  saveCart();
  renderCart();
}

function renderCart() {
  const el = document.querySelector('#cart-items');
  const count = cart.reduce((a, b) => a + b.qty, 0);
  document.querySelectorAll('.cart-count').forEach(x => { x.textContent = count; });
  if (!el) return;
  let total = 0;
  el.innerHTML = cart.map(i => {
    const p = productById(i.id);
    if (!p) return '';
    total += p.price * i.qty;
    return `<div class="cart-item"><img src="${esc(p.image || FALLBACK_IMAGE)}" onerror="safeImg(this)" alt=""><div><b>${esc(p.name)}</b><small>Qty ${i.qty} · <a href="#" data-remove="${esc(p.id)}">remove</a></small></div><span>${formatGBP(p.price * i.qty)}</span></div>`;
  }).join('') || '<p>Your bag is currently empty.</p>';
  const totalEl = document.querySelector('#cart-total');
  if (totalEl) totalEl.textContent = formatGBP(total);
}

function shell() {
  document.body.insertAdjacentHTML('beforeend', `<aside class="cart-drawer"><button class="close" onclick="this.parentElement.classList.remove('open')">×</button><div class="eyebrow">Your selection</div><h2 style="font-size:48px;margin:15px 0 35px">Shopping bag</h2><div id="cart-items"></div><hr><div style="display:flex;justify-content:space-between;margin:24px 0"><b>Total</b><b id="cart-total">£0</b></div><a class="btn" style="width:100%" href="/checkout">Secure checkout</a><p class="legal">Stock and pricing are verified by our server before Stripe opens.</p></aside>`);
  renderCart();
}

function renderGrid() {
  const grid = document.querySelector('[data-products]');
  if (!grid) return;
  const limit = Number(grid.dataset.products) || PRODUCTS.length;
  const shown = PRODUCTS.slice(0, limit);
  grid.innerHTML = shown.length
    ? shown.map(card).join('')
    : '<p>The boutique is being restocked. Please check back shortly.</p>';
  renderFilters(shown);
}

function renderFilters(shown) {
  const bar = document.querySelector('.filters');
  if (!bar) return;
  const categories = [...new Set(shown.map(p => p.category).filter(Boolean))].sort();
  bar.innerHTML = ['all', ...categories].map((cat, i) =>
    `<button class="filter${i === 0 ? ' active' : ''}" data-filter="${esc(cat)}">${esc(cat === 'all' ? 'All' : cat.charAt(0).toUpperCase() + cat.slice(1))}</button>`
  ).join('');
  bar.querySelectorAll('.filter').forEach(b => {
    b.onclick = () => {
      bar.querySelectorAll('.filter').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      document.querySelectorAll('.product-card').forEach(c => {
        c.style.display = b.dataset.filter === 'all' || c.dataset.cat === b.dataset.filter ? 'block' : 'none';
      });
    };
  });
}

async function renderDetail() {
  const detail = document.querySelector('#product-detail');
  if (!detail) return;
  const id = new URLSearchParams(location.search).get('id');
  let p = id ? productById(id) : null;

  if (!p && id) {
    // Not in the public list: it may be a draft an admin is previewing.
    try {
      const res = await fetch('/api/products/' + encodeURIComponent(id), { headers: { Accept: 'application/json' } });
      if (res.ok) { p = (await res.json()).product; }
    } catch (_) { /* handled below */ }
  }
  if (!p) p = PRODUCTS[0];
  if (!p) {
    detail.innerHTML = '<p>This product is no longer available. <a href="/shop">Return to the boutique</a>.</p>';
    return;
  }

  const stockNote = p.stock > 0 ? `${p.stock} currently in stock` : 'Currently out of stock';
  detail.innerHTML = `${p.active === false ? '<div class="notice" style="margin-bottom:20px"><b>Draft preview.</b><br>This product is hidden from the boutique until you publish it in the admin dashboard.</div>' : ''}
    <div class="detail-image"><img src="${esc(p.image || FALLBACK_IMAGE)}" onerror="safeImg(this)" alt="${esc(p.name)}"></div>
    <div class="detail-copy">
      <div class="eyebrow">${esc(p.brand)}</div>
      <h1>${esc(p.name)}</h1>
      <div class="price">${formatGBP(p.price)}</div>
      <p>${esc(p.description)}</p>
      <p class="legal" data-live-stock>${stockNote}</p>
      <button class="btn" data-add="${esc(p.id)}" ${p.stock === 0 ? 'disabled' : ''}>${p.stock === 0 ? 'Out of stock' : 'Add to bag'}</button>
      ${p.source ? `<p class="legal"><a href="${esc(p.source)}" rel="nofollow noopener" target="_blank">Manufacturer information</a></p>` : ''}
      <div class="notice"><b>Curated by Diamond Beauty.</b><br>For supplements, follow the product label and speak to a healthcare professional where appropriate. Product packaging may vary.</div>
    </div>`;
  document.title = `${p.name} | Diamond Beauty`;
}

function initPageFade() {
  const overlay = document.createElement('div');
  overlay.className = 'page-fade';
  overlay.setAttribute('aria-hidden', 'true');
  document.body.appendChild(overlay);
  requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add('is-ready')));
  document.addEventListener('click', e => {
    const link = e.target.closest('a[href]');
    if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const href = link.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('mailto:') || href.startsWith('tel:') || link.target === '_blank' || link.hasAttribute('download')) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin) return;
    e.preventDefault();
    overlay.classList.remove('is-ready');
    window.setTimeout(() => location.assign(url.href), 180);
  });
  window.addEventListener('pageshow', () => overlay.classList.add('is-ready'));
}

document.addEventListener('click', e => {
  const add = e.target.closest('[data-add]');
  if (add) { e.preventDefault(); addCart(add.dataset.add); return; }
  const remove = e.target.closest('[data-remove]');
  if (remove) { e.preventDefault(); removeFromCart(remove.dataset.remove); }
});

document.addEventListener('DOMContentLoaded', async () => {
  initPageFade();
  shell();
  await productsReady;
  renderGrid();
  renderCart();
  await renderDetail();
});
