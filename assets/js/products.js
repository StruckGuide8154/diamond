/* Catalogue loader. The catalogue lives in Redis and is edited from /admin,
   so the browser always asks the Flask server for it rather than shipping a
   hard-coded list. */
let PRODUCTS = [];

const productsReady = fetch('/api/products', { headers: { Accept: 'application/json' } })
  .then(res => (res.ok ? res.json() : { products: [] }))
  .then(data => {
    PRODUCTS = Array.isArray(data.products) ? data.products : [];
    return PRODUCTS;
  })
  .catch(err => {
    console.warn('Catalogue unavailable', err);
    PRODUCTS = [];
    return PRODUCTS;
  });
