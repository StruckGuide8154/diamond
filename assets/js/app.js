const formatGBP=n=>new Intl.NumberFormat('en-GB',{style:'currency',currency:'GBP'}).format(Number(n||0));
let cart=JSON.parse(localStorage.getItem('diamond-cart')||'[]');

function safeImg(img){
  if(!img)return;
  img.onerror=()=>{
    if(img.dataset.fallback&&img.src!==img.dataset.fallback){img.src=img.dataset.fallback}
    else{img.classList.add('image-failed');img.onerror=null}
  };
}

function productById(id){return PRODUCTS.find(x=>x.id===id)}

function card(p){
  const stockText=Number.isInteger(p.stock)?(p.stock>0?`${p.stock} in stock`:'Out of stock'):'';
  const disabled=p.stock===0?'disabled':'';
  return `<article class="product-card" data-cat="${p.category}" data-id="${p.id}">
    ${p.tag?`<span class="tag">${p.tag}</span>`:''}
    <a href="product.html?id=${p.id}">
      <div class="product-image"><img onerror="safeImg(this)" src="${p.img}" data-fallback="${p.fallback||'https://images.unsplash.com/photo-1598440947619-2c35fc9aa908?auto=format&fit=crop&w=800&q=80'}" alt="${p.name}"></div>
      <div class="product-meta"><div class="product-brand">${p.brand}</div><div class="product-title">${p.name}</div><div class="price">${formatGBP(p.price)}</div><small class="stock-label">${stockText}</small></div>
    </a>
    <button class="quick-add" ${disabled} onclick="addCart('${p.id}')" aria-label="Add to bag">${p.stock===0?'×':'+'}</button>
  </article>`;
}

function addCart(id){
  const p=productById(id);
  if(!p)return;
  const x=cart.find(i=>i.id===id);
  const current=x?x.qty:0;
  if(Number.isInteger(p.stock)&&current>=p.stock){
    alert(p.stock===0?'This item is currently out of stock.':`Only ${p.stock} available.`);
    return;
  }
  x?x.qty++:cart.push({id,qty:1});
  localStorage.setItem('diamond-cart',JSON.stringify(cart));
  renderCart();
  document.querySelector('.cart-drawer')?.classList.add('open');
}

function renderCart(){
  const el=document.querySelector('#cart-items');
  const count=cart.reduce((a,b)=>a+b.qty,0);
  document.querySelectorAll('.cart-count').forEach(x=>x.textContent=count);
  if(!el)return;
  let total=0;
  el.innerHTML=cart.map(i=>{
    const p=productById(i.id);
    if(!p)return '';
    total+=p.price*i.qty;
    return `<div class="cart-item"><img src="${p.img}" onerror="safeImg(this)" data-fallback="${p.fallback||''}"><div><b>${p.name}</b><small>Qty ${i.qty}</small></div><span>${formatGBP(p.price*i.qty)}</span></div>`;
  }).join('')||'<p>Your bag is currently empty.</p>';
  document.querySelector('#cart-total').textContent=formatGBP(total);
}

function shell(){
  document.body.insertAdjacentHTML('beforeend',`<aside class="cart-drawer"><button class="close" onclick="this.parentElement.classList.remove('open')">×</button><div class="eyebrow">Your selection</div><h2 style="font-size:48px;margin:15px 0 35px">Shopping bag</h2><div id="cart-items"></div><hr><div style="display:flex;justify-content:space-between;margin:24px 0"><b>Total</b><b id="cart-total">£0</b></div><a class="btn" style="width:100%" href="checkout.html">Secure checkout</a><p class="legal">Stock and pricing are verified by our server before Stripe opens.</p></aside>`);
  renderCart();
}

async function hydrateStock(){
  try{
    const res=await fetch('/api/products',{headers:{Accept:'application/json'}});
    if(!res.ok)return;
    const data=await res.json();
    const byId=new Map((data.products||[]).map(p=>[p.id,p]));
    PRODUCTS.forEach(p=>{
      const live=byId.get(p.id);
      if(live){
        p.stock=live.stock;
        p.price=live.price;
      }
    });
    document.querySelectorAll('.product-card').forEach(el=>{
      const p=productById(el.dataset.id);
      if(!p)return;
      const label=el.querySelector('.stock-label');
      const btn=el.querySelector('.quick-add');
      if(label)label.textContent=p.stock>0?`${p.stock} in stock`:'Out of stock';
      if(btn){
        btn.disabled=p.stock===0;
        btn.textContent=p.stock===0?'×':'+';
      }
      const price=el.querySelector('.price');
      if(price)price.textContent=formatGBP(p.price);
    });
    const detail=document.querySelector('#product-detail');
    if(detail){
      const p=productById(new URLSearchParams(location.search).get('id'))||PRODUCTS[0];
      const notice=detail.querySelector('[data-live-stock]');
      if(notice)notice.textContent=p.stock>0?`${p.stock} currently in stock`:'Currently out of stock';
      const btn=detail.querySelector('.btn');
      if(btn)btn.disabled=p.stock===0;
      const price=detail.querySelector('.price');
      if(price)price.textContent=formatGBP(p.price);
    }
    renderCart();
  }catch(err){console.warn('Live inventory unavailable',err)}
}

function initPageFade(){
  const overlay=document.createElement('div');
  overlay.className='page-fade';
  overlay.setAttribute('aria-hidden','true');
  document.body.appendChild(overlay);
  requestAnimationFrame(()=>requestAnimationFrame(()=>overlay.classList.add('is-ready')));
  document.querySelectorAll('a[href]').forEach(link=>{
    const href=link.getAttribute('href');
    if(!href||href.startsWith('#')||href.startsWith('mailto:')||href.startsWith('tel:'))return;
    try{const url=new URL(link.href,location.href);if(url.origin===location.origin)link.rel='prefetch'}catch(_){}
  });
  document.addEventListener('click',e=>{
    const link=e.target.closest('a[href]');
    if(!link||e.defaultPrevented||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
    const href=link.getAttribute('href');
    if(!href||href.startsWith('#')||href.startsWith('mailto:')||href.startsWith('tel:')||link.target==='_blank'||link.hasAttribute('download'))return;
    const url=new URL(link.href,location.href);
    if(url.origin!==location.origin)return;
    e.preventDefault();
    overlay.classList.remove('is-ready');
    window.setTimeout(()=>location.assign(url.href),180);
  });
  window.addEventListener('pageshow',()=>overlay.classList.add('is-ready'));
}

document.addEventListener('DOMContentLoaded',()=>{
  initPageFade();
  shell();
  const grid=document.querySelector('[data-products]');
  if(grid)grid.innerHTML=PRODUCTS.slice(0,grid.dataset.products||PRODUCTS.length).map(card).join('');
  document.querySelectorAll('.filter').forEach(b=>b.onclick=()=>{
    document.querySelectorAll('.filter').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    document.querySelectorAll('.product-card').forEach(c=>c.style.display=b.dataset.filter==='all'||c.dataset.cat===b.dataset.filter?'block':'none');
  });
  const detail=document.querySelector('#product-detail');
  if(detail){
    const p=productById(new URLSearchParams(location.search).get('id'))||PRODUCTS[0];
    detail.innerHTML=`<div class="detail-image"><img src="${p.img}" data-fallback="${p.fallback||''}" onerror="safeImg(this)" alt="${p.name}"></div><div class="detail-copy"><div class="eyebrow">${p.brand}</div><h1>${p.name}</h1><div class="price">${formatGBP(p.price)}</div><p>${p.desc}</p><p class="legal" data-live-stock>Checking stock…</p><div class="qty"><input type="number" min="1" value="1"></div><button class="btn" onclick="addCart('${p.id}')">Add to bag</button><div class="notice"><b>Curated by Diamond Beauty.</b><br>For supplements, follow the product label and speak to a healthcare professional where appropriate. Product packaging may vary.</div></div>`;
  }
  hydrateStock();
});
