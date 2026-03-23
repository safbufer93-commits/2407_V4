#!/usr/bin/env python3
"""
Main entry point for 2407.pl fitment crawler.
Uses Dolphin Anty antidect browser to bypass Cloudflare.

Usage:
    python main.py [options]

Options:
    --no-sitemap        Skip sitemap discovery
    --sections S [S ..] Only process listed sections (e.g. Фильтры Автосвет)
    --limit N           Stop after N products total (for testing)
    --limit-per-seed N  Stop after N products per seed URL (for testing)
    --output-dir DIR    Output directory (default: ./output)
    --csv               Also write CSV output
    --log-level LEVEL   DEBUG, INFO, WARNING (default: INFO)
"""
import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    SEED_URLS,
    BASE_URL,
    SITEMAP_URL,
    OUTPUT_DIR,
    OUTPUT_BASE_NAME,
    ROW_LIMIT,
    LOG_DIR,
    LOG_LEVEL,
    DOLPHIN_PROFILE_ID,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
    MAX_RETRIES,
)
from src.logger import setup_logging, Metrics
from src.crawler import CategoryCrawler, SitemapParser
from src.extractor import extract_product
from src.exporter import (
    RotatingXlsxWriter,
    CsvWriter,
    MAX_ORIGINAL_PAIRS,
    MAX_ANALOG_PAIRS,
)
from src.renderer import RendererUnavailableError

logger = logging.getLogger(__name__)


def load_categories_file(path: str) -> list:
    """Load category URLs from a CSV or plain text file."""
    entries = []

    def _slug_from_url(url: str) -> str:
        return url.rstrip("/").split("/")[-1] or "category"

    def _append_entry(url: str, section: str = "Категории", subsection: str = ""):
        if not url or not url.startswith("http"):
            return
        sec = (section or "").strip() or "Категории"
        sub = (subsection or "").strip() or _slug_from_url(url)
        entries.append({"url": url.strip(), "section": sec, "subsection": sub})

    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        is_csv = any(sep in sample for sep in (",", ";", "\t"))

        if is_csv:
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
                dialect.delimiter = ";"

            fh.seek(0)
            reader = csv.DictReader(fh, dialect=dialect)
            if reader.fieldnames:
                field_map = {f.strip().lower(): f for f in reader.fieldnames if f}
                url_field = next(
                    (field_map[k] for k in ("url", "loc", "address", "link") if k in field_map),
                    None,
                )
                if url_field is None:
                    url_field = reader.fieldnames[0]

                section_field = next(
                    (field_map[k] for k in ("section", "category", "категория", "раздел") if k in field_map),
                    None,
                )
                subsection_field = next(
                    (field_map[k] for k in ("subsection", "subcategory", "подраздел") if k in field_map),
                    None,
                )

                for row in reader:
                    raw = (row.get(url_field) or "").strip()
                    sec = (row.get(section_field) or "").strip() if section_field else "Категории"
                    sub = (row.get(subsection_field) or "").strip() if subsection_field else ""
                    _append_entry(raw, sec, sub)

            if not entries:
                fh.seek(0)
                for line in fh:
                    raw = line.strip().strip('"')
                    if raw.startswith("http"):
                        _append_entry(raw)
                    else:
                        for token in re.split(r"[,\t;]", raw):
                            token = token.strip().strip('"')
                            if token.startswith("http"):
                                _append_entry(token)
                                break
        else:
            for line in fh:
                raw = line.strip()
                _append_entry(raw)

    logger.info(f"Loaded {len(entries)} category URLs from {path}")
    return entries


def parse_args():
    parser = argparse.ArgumentParser(description="2407.pl fitment crawler (Dolphin Anty)")
    parser.add_argument("--no-sitemap", action="store_true")
    parser.add_argument(
        "--sections",
        nargs="+",
        default=[],
        help="Разделы для парсинга, например: --sections Фильтры Автосвет",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--limit-per-seed",
        type=int,
        default=0,
        help="Stop after N products per seed URL (for testing)",
    )
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--csv", action="store_true")
    parser.add_argument("--log-level", default=LOG_LEVEL)
    parser.add_argument(
        "--categories-file",
        default=None,
        help="Path to CSV/TXT with category URLs (skips seed-based discovery). "
        "Auto-detected if config/sitemap-category-ru.csv exists.",
    )
    parser.add_argument(
        "--resume-file",
        default=None,
        help="Path to resume checkpoint JSON (default: ./logs/resume_checkpoint.json)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore existing resume checkpoint and do not save a new one",
    )
    parser.add_argument(
        "--renderer-recover-retries",
        type=int,
        default=int(os.environ.get("RENDERER_RECOVER_RETRIES", "2")),
        help="How many times to recover renderer and retry same product on renderer outage",
    )
    parser.add_argument(
        "--renderer-recover-wait",
        type=int,
        default=int(os.environ.get("RENDERER_RECOVER_WAIT", "10")),
        help="Seconds to wait before renderer recovery attempt",
    )
    return parser.parse_args()


def _default_resume_file() -> str:
    return os.path.join(LOG_DIR, "resume_checkpoint.json")


def load_resume_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"Could not load resume checkpoint {path}: {e}")
    return {}


def save_resume_state(path: str, state: dict):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        logger.warning(
            f"Saved resume checkpoint: product={state.get('product_url')} source={state.get('source_url')}"
        )
    except Exception as e:
        logger.error(f"Could not save resume checkpoint {path}: {e}")


def clear_resume_state(path: str):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"Cleared resume checkpoint: {path}")
    except Exception as e:
        logger.warning(f"Could not clear resume checkpoint {path}: {e}")


def recover_renderer(renderer, wait_seconds: int = 10) -> bool:
    try:
        renderer.close()
    except Exception:
        pass

    time.sleep(max(1, wait_seconds))

    try:
        renderer.setup_poland()
        return True
    except Exception as e:
        logger.warning(f"Renderer recovery attempt failed: {e}")
        return False


def build_renderer():
    from src.renderer import AdaptiveRenderer

    return AdaptiveRenderer(
        profile_id=DOLPHIN_PROFILE_ID,
        delay_min=REQUEST_DELAY_MIN,
        delay_max=REQUEST_DELAY_MAX,
        max_retries=MAX_RETRIES,
    )


def process_product(product_info: dict, renderer, metrics: Metrics) -> list:
    url = product_info["product_url"]
    source_ctx = {
        "source_section": product_info["source_section"],
        "source_subsection": product_info["source_subsection"],
        "source_url": product_info["source_url"],
    }

    t0 = time.time()
    try:
        html = renderer.fetch_html(url)
    except RendererUnavailableError:
        raise
    except Exception as e:
        metrics.record_error("fetch_exception")
        logger.warning(f"Fetch exception for {url}: {e}")
        return []

    elapsed = (time.time() - t0) * 1000

    if not html:
        metrics.record_error("fetch_failed")
        logger.warning(f"Failed to fetch: {url}")
        return []

    metrics.record_page("dolphin", elapsed)

    try:
        product_data = extract_product(url, html)
    except Exception as e:
        metrics.record_error("parse_error")
        logger.error(f"Parse error for {url}: {e}")
        return []

    has_required = all(
        [
            product_data.product_id is not None,
            product_data.name,
            product_data.brand,
            product_data.part_number_display,
            product_data.price_pln is not None,
        ]
    )

    has_fitment = any(
        (r.make is not None)
        or (r.model is not None)
        or (getattr(r, "model_type", None) is not None)
        or (getattr(r, "modification", None) is not None)
        for r in product_data.fitment_rows
    )
    metrics.record_product(has_fitment, has_required)

    fitment_rows = product_data.fitment_rows or []
    original_rows = product_data.original_numbers or []
    analog_rows = product_data.analog_rows or []

    row_count = max(len(fitment_rows), 1)

    def _pick(rows: list, idx: int):
        if not rows:
            return None
        if idx < len(rows):
            return rows[idx]
        if len(rows) == 1:
            return rows[0]
        return None

    def _pair_key(prefix: str, item_type: str, idx: int) -> str:
        suffix = "" if idx == 0 else str(idx + 1)
        return f"{prefix}_{item_type}{suffix}"

    def _attach_pair_columns(row: dict, prefix: str, items: list, max_pairs: int):
        for idx in range(max_pairs):
            brand_key = _pair_key(prefix, "brand", idx)
            number_key = _pair_key(prefix, "number", idx)
            if idx < len(items):
                item = items[idx]
                row[brand_key] = getattr(item, "brand", None)
                row[number_key] = getattr(item, "number", None)
            else:
                row[brand_key] = None
                row[number_key] = None

    rows = []
    for idx in range(row_count):
        fitment = _pick(fitment_rows, idx)

        row = {
            "source_section": source_ctx["source_section"],
            "source_subsection": source_ctx["source_subsection"],
            "source_url": source_ctx["source_url"],
            "breadcrumb_path": product_data.breadcrumb_path,
            "product_url": url,
            "product_id": product_data.product_id,
            "name": product_data.name,
            "brand": product_data.brand,
            "part_number_display": product_data.part_number_display,
            "part_number_normalized": product_data.part_number_normalized,
            "price_pln": product_data.price_pln,
            "vat_included": product_data.vat_included,
            "characteristics": product_data.characteristics,
            "fitment_make": fitment.make if fitment else None,
            "fitment_model": fitment.model if fitment else None,
            "fitment_model_type": fitment.model_type if fitment else None,
            "fitment_modification": fitment.modification if fitment else None,
            "fitment_raw_line": fitment.raw_line if fitment else None,
        }
        _attach_pair_columns(row, "original", original_rows, MAX_ORIGINAL_PAIRS)
        _attach_pair_columns(row, "analog", analog_rows, MAX_ANALOG_PAIRS)
        rows.append(row)

    metrics.record_rows(len(rows))
    logger.debug(f"Product {url}: {len(rows)} rows, fitment={has_fitment}")
    return rows


def main():
    args = parse_args()
    setup_logging(LOG_DIR, args.log_level)
    logger.info("2407.pl fitment crawler starting (Dolphin Anty mode)")

    resume_file = args.resume_file or _default_resume_file()
    resume_state = {}
    if args.no_resume:
        logger.info("Resume is disabled via --no-resume")
    else:
        resume_state = load_resume_state(resume_file)
        if resume_state.get("product_url"):
            logger.info(
                "Resume checkpoint loaded: "
                f"product={resume_state.get('product_url')} "
                f"source={resume_state.get('source_url')}"
            )

    resume_active = bool(resume_state.get("product_url")) and not args.no_resume
    resume_hit = False
    terminated_due_renderer = False
    completed_without_fatal = False

    renderer = build_renderer()

    logger.info("Setting up Poland/PLN context...")
    renderer.setup_poland()

    output_dir = args.output_dir
    xlsx_writer = RotatingXlsxWriter(output_dir, OUTPUT_BASE_NAME, ROW_LIMIT)
    csv_writer = CsvWriter(output_dir, OUTPUT_BASE_NAME, ROW_LIMIT) if args.csv else None
    metrics = Metrics()

    if not args.no_sitemap:
        logger.info("Trying sitemap...")
        try:
            parser = SitemapParser(renderer)
            prefixes = list(set(s["url"].replace("https://2407.pl", "") for s in SEED_URLS))
            sitemap_urls = parser.get_urls_for_sections(SITEMAP_URL, prefixes)
            logger.info(f"Sitemap: {len(sitemap_urls)} URLs")
        except Exception as e:
            logger.warning(f"Sitemap failed: {e}")

    crawler = CategoryCrawler(renderer, base_url=BASE_URL)

    categories_file = args.categories_file
    if not categories_file:
        auto_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "config",
            "sitemap-category-ru.csv",
        )
        if os.path.exists(auto_path):
            categories_file = auto_path
            logger.info(f"Auto-detected categories file: {auto_path}")

    seen_per_source = {}  # source_url -> set of product_url

    def _iter_products(seed_or_cat):
        if categories_file:
            return crawler.crawl_category_direct(
                seed_or_cat["url"],
                seed_or_cat["section"],
                seed_or_cat["subsection"],
            )
        return crawler.crawl_seed(seed_or_cat)

    products_count = 0

    try:
        if categories_file:
            active_items = load_categories_file(categories_file)
            logger.info(f"Categories-file mode: {len(active_items)} categories")
        else:
            active_items = [
                s for s in SEED_URLS if not args.sections or s["section"] in args.sections
            ]

        for seed in active_items:
            logger.info(f"Processing: {seed['section']} — {seed['url']}")
            seed_count = 0

            product_iter = iter(_iter_products(seed))
            last_product_info = None

            while True:
                try:
                    product_info = next(product_iter)
                    last_product_info = product_info
                except StopIteration:
                    break
                except RendererUnavailableError:
                    if not args.no_resume:
                        checkpoint = {
                            "saved_at": datetime.now().isoformat(timespec="seconds"),
                            "source_section": (last_product_info or {}).get("source_section", seed["section"]),
                            "source_subsection": (last_product_info or {}).get("source_subsection", ""),
                            "source_url": (last_product_info or {}).get("source_url", seed["url"]),
                            "product_url": (last_product_info or {}).get("product_url", ""),
                            "error": "renderer_unavailable_during_listing",
                        }
                        save_resume_state(resume_file, checkpoint)
                    terminated_due_renderer = True
                    raise
                except Exception as e:
                    metrics.record_error("listing_iteration_fatal")
                    logger.error(f"Listing iteration fatal for {seed['url']}: {e}", exc_info=True)
                    break

                src_url = product_info["source_url"]
                prod_url = product_info["product_url"]

                if src_url not in seen_per_source:
                    seen_per_source[src_url] = set()
                if prod_url in seen_per_source[src_url]:
                    continue

                # Resume: skip everything before the checkpoint product.
                if resume_active:
                    target_url = resume_state.get("product_url")
                    if prod_url != target_url:
                        seen_per_source[src_url].add(prod_url)
                        continue
                    target_source = resume_state.get("source_url")
                    if target_source and src_url != target_source:
                        logger.warning(
                            "Resume product matched with a different source URL: "
                            f"saved={target_source}, current={src_url}"
                        )
                    resume_active = False
                    resume_hit = True
                    logger.info(f"Resume checkpoint reached at product: {prod_url}")

                seen_per_source[src_url].add(prod_url)

                recover_attempt = 0
                max_recover = max(0, args.renderer_recover_retries)
                rows = None
                while True:
                    try:
                        rows = process_product(product_info, renderer, metrics)
                        break
                    except RendererUnavailableError as e:
                        checkpoint = {
                            "saved_at": datetime.now().isoformat(timespec="seconds"),
                            "source_section": product_info.get("source_section"),
                            "source_subsection": product_info.get("source_subsection"),
                            "source_url": src_url,
                            "product_url": prod_url,
                            "error": str(e),
                        }
                        if not args.no_resume:
                            save_resume_state(resume_file, checkpoint)

                        if recover_attempt >= max_recover:
                            terminated_due_renderer = True
                            raise

                        recover_attempt += 1
                        metrics.record_error("renderer_unavailable_retry")
                        logger.warning(
                            f"Renderer unavailable at {prod_url}. "
                            f"Recovery attempt {recover_attempt}/{max_recover}..."
                        )

                        if not recover_renderer(renderer, args.renderer_recover_wait):
                            continue
                        logger.info(f"Renderer recovered, retrying product: {prod_url}")
                    except Exception as e:
                        metrics.record_error("product_processing_fatal")
                        logger.error(f"Product processing fatal for {prod_url}: {e}", exc_info=True)
                        rows = []
                        break

                for row in rows:
                    xlsx_writer.write_row(row)
                    if csv_writer:
                        csv_writer.write_row(row)

                products_count += 1
                seed_count += 1

                if args.limit_per_seed and seed_count >= args.limit_per_seed:
                    logger.info(
                        f"Reached per-seed limit ({args.limit_per_seed}) for: {seed['url']}"
                    )
                    break

                if args.limit and products_count >= args.limit:
                    logger.info(f"Reached limit: {args.limit}")
                    break

                if products_count % 50 == 0:
                    s = metrics.summary()
                    logger.info(
                        f"Progress: {products_count} products, "
                        f"{s['rows_written']} rows, "
                        f"{s['pct_fitment_found']:.1f}% fitment"
                    )

            if args.limit and products_count >= args.limit:
                break

        if resume_active and not resume_hit and not args.no_resume:
            logger.warning(
                "Resume checkpoint product was not found in current run. "
                "Starting from the beginning may be required."
            )

        completed_without_fatal = True

    except KeyboardInterrupt:
        logger.info("Interrupted")
    except RendererUnavailableError as e:
        terminated_due_renderer = True
        logger.error(f"Renderer unavailable, stopping run: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Fatal: {e}", exc_info=True)
    finally:
        xlsx_writer.finalize()
        if csv_writer:
            csv_writer.finalize()
        renderer.close()

        os.makedirs(LOG_DIR, exist_ok=True)
        report = os.path.join(
            LOG_DIR,
            f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        metrics.save_report(report)
        metrics.print_summary()
        logger.info(f"Done. Output: {output_dir}")
        if not args.no_resume and completed_without_fatal and not terminated_due_renderer:
            clear_resume_state(resume_file)


if __name__ == "__main__":
    main()
