/* Catalogue loader. The catalogue lives in Redis and is edited from /admin,
   so the browser always asks the Flask server for it rather than shipping a
   hard-coded list. */
let PRODUCTS = [];
let CATEGORIES = [];

function loadCatalogueEndpoint(url, key) {
  return fetch(url, { headers: { Accept: 'application/json' } })
    .then(res => (res.ok ? res.json() : { [key]: [] }))
    .then(data => (Array.isArray(data[key]) ? data[key] : []))
    .catch(err => {
      console.warn(`${key} unavailable`, err);
      return [];
    });
}

const productsReady = Promise.all([
  loadCatalogueEndpoint('/api/products', 'products'),
  loadCatalogueEndpoint('/api/categories', 'categories')
]).then(([products, categories]) => {
  PRODUCTS = products;
  CATEGORIES = categories;
  return PRODUCTS;
});
