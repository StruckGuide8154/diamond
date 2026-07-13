# Shopify connection

This build is a polished storefront prototype. Product checkout becomes live after the Diamond Beauty Shopify store and its product variants exist.

1. Create the products in Shopify using the names, images, prices and descriptions in `assets/js/products.js`.
2. Add a `variantId` to every product object. Example: `variantId:'45678901234567'`.
3. Set the shop domain in `assets/js/app.js`, for example `const SHOP='diamond-beauty.myshopify.com';`.
4. Replace the checkout link with a Shopify cart permalink assembled as:
   `https://SHOP_DOMAIN/cart/VARIANT_ID:QUANTITY,VARIANT_ID:QUANTITY`
5. For a fully native theme, move the layout into Shopify Liquid sections and replace the JavaScript catalogue with `collections` and `product` objects.

Do not copy supplier claims blindly. Check product packaging, ingredients, prices, stock, image rights, UK supplement labelling, VAT and fulfilment before launch.
