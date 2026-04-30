# AI Product Image Enrichment

AI-driven, manufacturer-agnostic product image enrichment for Odoo 19 Community Edition.

**Main images are transparent PNGs**: every product is isolated, background-removed,
and centered on a uniform 1920×1920 transparent canvas with consistent padding.
The product floats over whatever your theme's card background is, identical in size
and position across the entire shop grid.

**Gallery images keep their original backgrounds.** A floor scrubber in a school
hallway should look like a floor scrubber in a school hallway. Lifestyle/in-use
shots are added as gallery images that appear after a customer clicks into the
product page — never as the main thumbnail.

## Why this is different

Traditional scraper modules ship with hardcoded CSS/XPath rules per manufacturer.
You can't maintain that across dozens of brands. **This module sends candidate pages
to Claude and asks it to identify product images the way a human would** — so it
adapts to new manufacturers automatically with zero rule-writing.

## Pipeline

1. **Discover pages**: try the manufacturer's `sitemap.xml` first (free, polite).
   Fall back to web search (Brave / SerpAPI / Google CSE / DuckDuckGo HTML).
2. **Fetch** the candidate manufacturer page (robots.txt-respecting, rate-limited).
   For SPA / JS-rendered sites, optionally render via Browserless to get the real DOM.
3. **Classify**:
   - If the domain has a learned **scraping recipe** (built after 5 successful AI
     classifications), extract images directly via CSS selectors. Free.
   - Otherwise, send `<img>` inventory to Claude. Strict white-BG bias for `role=main`.
4. **Detect background** — numpy heuristic for the obvious cases; Claude vision
   disambiguates borderline images (border whiteness 70-90%).
5. **Remove background** if needed — Photoroom API by default (recommended), local
   rembg as fallback.
6. **Normalize** — trim to content, resize proportionally to a target inner area,
   center on a uniform white canvas with consistent padding.
7. **Hash signature** stored on the product. Re-running with the same image and
   settings is a no-op — no quality loss from repeated JPEG re-encoding.

## Installation on CloudPepper

1. Push this folder to your CloudPepper-tracked GitHub branch.
2. Trigger a redeploy on the CloudPepper dashboard.
3. Apps → Update Apps List → install **AI Product Image Enrichment**.

### Python dependencies

These need to live in the **Odoo Python interpreter**, not system pip:

```
sudo -u odoo /opt/odoo/venv/bin/pip install \
    requests beautifulsoup4 lxml Pillow numpy anthropic
```

(Path may differ — check CloudPepper's docs for your exact venv location.)

**Background removal:** the recommended path is to use the Photoroom API
(set `Photoroom API Key` in Settings — ~$0.02/image, no local dependencies).
**If you prefer self-hosted rembg**, also install:

```
sudo -u odoo /opt/odoo/venv/bin/pip install rembg onnxruntime
```

Be aware that rembg + onnxruntime is ~400MB installed and needs ~500MB RAM at
inference; some CloudPepper plans will OOM. For your full-catalog backfill
(500 products), Photoroom costs ~$10 total — not worth fighting CloudPepper memory.

### First-run setup

1. **Settings → Sales → AI Image Enrichment**
2. Paste your Anthropic API key. Default model: `claude-haiku-4-5-20251001`.
3. Paste your **Photoroom API key** (recommended). If left empty, the module
   falls back to local rembg.
4. (Optional) Paste a **Browserless API key** if you anticipate hitting
   SPA-rendered manufacturer sites (Tennant, parts of Karcher).
5. Pick a search provider and paste its key. Brave is recommended
   (free tier 2,000 queries/month).
6. If you skipped Photoroom and use rembg: click **Pre-warm rembg model** —
   this downloads ~150MB of model weights.
7. Click **Preview Normalization** — upload 3-4 sample existing product
   images of different types (white-bg studio, tight crop, in-context).
   Tune `padding_percent` and `target_canvas_size` until the output looks right.
   Click **Save Settings as Defaults**.

## Recommended workflow

1. **Tune** with Preview Normalization wizard (steps above).
2. **Normalize 10 products** via the Normalize wizard, selection mode = Selected.
   Open the shop. Verify the grid looks uniform.
3. **Normalize one category.** Spot-check.
4. **Normalize the full catalog.** Cron processes 5 products / minute, so 500
   products takes ~100 minutes.
5. Move to **AI enrichment**: run Discover-only on 5 products, review candidates,
   tune the confidence threshold.
6. Scale enrichment up.

## Models

| Model | Purpose |
|---|---|
| `aipie.enrichment.job` | Batch job, processed 5 products / cron tick |
| `aipie.product.image.candidate` | AI-discovered image awaiting review |
| `aipie.enrichment.log` | Per-step audit trail |
| `aipie.ai.usage.log` | Anthropic API usage + cost |
| `aipie.scraping.recipe` | Per-domain learned CSS selectors (self-built after 5 AI successes) |

## XML-RPC API

```python
models.execute_kw(db, uid, pw, 'aipie.enrichment.job', 'aipie_enrich_by_skus',
                  [['SKU-1', 'SKU-2'], 'discover_only'])
models.execute_kw(db, uid, pw, 'aipie.enrichment.job',
                  'aipie_normalize_existing_images', [None])  # all
```

## Troubleshooting

* **rembg fails to load model:** the first call downloads ~150MB. Check that
  outbound HTTPS works and `~/.u2net/` (or wherever rembg caches) is writable.
* **Anti-bot block:** Cloudflare/Akamai-protected manufacturer sites are skipped
  intentionally. The module never tries to bypass these. You'll see "no_results"
  for affected products — upload manually.
* **Low confidence scores everywhere:** lower `min_confidence_score` to 0.6, or
  switch the model to `claude-sonnet-4-6` for a smarter classifier.
* **French character encoding in product names:** Postgres + Odoo handle UTF-8
  natively. If you see `?` in API output, your client is the problem.
* **White-BG detection misfires** (e.g. product is rejected as "non-white" when
  it isn't): open Preview Normalization, upload the image, lower
  `white_bg_min_percent` from 85 to 75 or raise `white_threshold` to 250.

## Privacy & legal

* Respects robots.txt — non-negotiable.
* Identifies itself with a descriptive User-Agent.
* Downloads images for legitimate distributor use only (you're an authorized
  reseller of the brands you list).

## Anti-footgun defaults

* Original main image is backed up before any modification.
* Manual review queue is the default; auto-apply is opt-in.
* Confidence threshold defaults to 0.7.
* Monthly Anthropic budget defaults to $50 — alerts at 80%, paused at 100%.
* Per-domain rate limit defaults to 2 seconds.
* Idempotent: re-running on an enriched product is a no-op unless overwrite is on.

## Known limitations

* CloudPepper memory: rembg + onnxruntime on tight RAM plans can OOM.
  Switch to `u2net` or sidecar.
* DuckDuckGo HTML provider is unreliable — use Brave in production.
* Manufacturer sites that disallow crawling in robots.txt will return
  no results. By design.
