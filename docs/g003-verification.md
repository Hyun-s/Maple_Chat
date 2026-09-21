# G003 Verification Evidence

- Sanitized offline fixtures cover board IDs 2294–2298, 2300, and 2304.
- Listing/category/article parsing, `#powerbbsContent`, comment/reply trees, media metadata, and
  parser drift are regression tested without live HTTP.
- Email, Korean phone numbers, Discord mentions/tags, and nicknames are masked or irreversibly
  HMAC-hashed before persistence shapes are created.
- Image MIME/byte/pixel bounds, OCR timeout/confidence, and OCR PII masking are enforced.
- The optional `TesseractOCRProvider` runs `kor+eng` recognition locally, normalizes per-word
  confidence, closes image resources, and remains behind the bounded/sanitizing OCR boundary.
- Repeated 403/429, CAPTCHA, and parser drift open a fail-closed circuit; external/video links are
  metadata only and are never recursively fetched.

No live Inven request was made.
