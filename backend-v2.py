from flask import Flask, request, jsonify
from flask_cors import CORS
from elasticsearch import Elasticsearch, exceptions
import asyncio
import json
import re
import os
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import urllib3

# http://localhost:5000/api/products?q (get)
# http://localhost:5000/api/crawl (post)

# --- Config ---
ES_HOST = "https://localhost:9200"
ES_USER = "elastic"
ES_PASS = "OZSevqpq3n6RTbD8ew-_"
INDEX = "products"
TEMP_SAVE_FILENAME = "apniroots_products_partial.json"
OUTPUT_FILENAME = "apniroots_products.json"

# --- Flask Setup ---
app = Flask(__name__)
CORS(app)

# --- Elasticsearch Setup ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
es = Elasticsearch(
    ES_HOST,
    basic_auth=(ES_USER, ES_PASS),
    verify_certs=False
)

# --- Utility ---
def parse_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", price_str)
    try:
        return float(cleaned)
    except ValueError:
        return None


async def scrape_apniroots():
    url = "https://apniroots.com/collections/all"
    products_data = []
    existing_product_names = set()
    MAX_NO_CHANGE_SCROLLS = 5
    NETWORK_IDLE_TIMEOUT = 10000
    SCROLL_PAUSE_TIME = 1.0
    MAX_PRODUCTS = 400  

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)

            try:
                await page.wait_for_selector('div[data-testid="POPUP"]', timeout=7000)
                await page.click('button[aria-label="Close dialog"]', timeout=3000)
                await asyncio.sleep(1)
                await page.wait_for_selector('div[data-testid="POPUP"]', state='hidden', timeout=5000)
            except PlaywrightTimeout:
                pass
            except Exception:
                await page.keyboard.press('Escape')
                await asyncio.sleep(1)

            print("Scrolling to load products (up to 400)...")
            last_height = await page.evaluate("document.body.scrollHeight")
            current_product_count_on_page = len(await page.query_selector_all('product-item.product-collection'))
            no_change_scrolls = 0

            while True:
                if len(products_data) >= MAX_PRODUCTS:
                    print(f"Reached limit of {MAX_PRODUCTS} products. Stopping.")
                    break

                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try:
                    await page.wait_for_load_state('networkidle', timeout=NETWORK_IDLE_TIMEOUT)
                except PlaywrightTimeout:
                    pass
                await asyncio.sleep(SCROLL_PAUSE_TIME)
                new_height = await page.evaluate("document.body.scrollHeight")

                all_product_elements_on_page = await page.query_selector_all('product-item.product-collection')
                new_product_count_on_page = len(all_product_elements_on_page)

                print(f"Scrolled. Visible: {new_product_count_on_page}. Collected: {len(products_data)}")

                if new_height == last_height and new_product_count_on_page == current_product_count_on_page:
                    no_change_scrolls += 1
                    if no_change_scrolls >= MAX_NO_CHANGE_SCROLLS:
                        print("No new products detected. Ending.")
                        break
                else:
                    no_change_scrolls = 0

                last_height = new_height
                current_product_count_on_page = new_product_count_on_page

                for product_elem in all_product_elements_on_page:
                    if len(products_data) >= MAX_PRODUCTS:
                        break

                    name_element = await product_elem.query_selector('h4 a')
                    product_name = await name_element.text_content() if name_element else None
                    if not product_name or product_name in existing_product_names:
                        continue

                    existing_product_names.add(product_name)
                    product = {"name": product_name}

                    price_sale = await product_elem.query_selector('span.price--sale[data-js-product-price]')
                    price_regular = await product_elem.query_selector('span.price[data-js-product-price]')
                    raw_price = await price_sale.text_content() if price_sale else (
                        await price_regular.text_content() if price_regular else None)
                    product['price'] = parse_price(raw_price)

                    desc_element = await product_elem.query_selector('p.product-collection__description')
                    product['description'] = (await desc_element.text_content()).strip() if desc_element else None
                    product['rating'] = None
                    product['category'] = "All Products"

                    availability_element = await product_elem.query_selector('p[data-js-product-availability] span:nth-child(2)')
                    availability_text = await availability_element.text_content() if availability_element else ""
                    if "In Stock" in availability_text:
                        product['availability'] = "In Stock"
                    elif "Sold Out" in availability_text:
                        product['availability'] = "Sold Out"
                    else:
                        product['availability'] = "Unknown"

                    img_element = await product_elem.query_selector('img.rimage__img')
                    if img_element:
                        data_master_url = await img_element.get_attribute('data-master')
                        if data_master_url:
                            product['image_url'] = 'https:' + data_master_url.replace('{width}x', '1024x') \
                                if not data_master_url.startswith('http') else data_master_url
                        else:
                            product['image_url'] = None
                    else:
                        product['image_url'] = None

                    products_data.append(product)

        except Exception as e:
            print(f"Scraping error: {e}")
        finally:
            await browser.close()

    print(f"✅ Scraped {len(products_data)} products (capped at {MAX_PRODUCTS})")
    return products_data

def create_index():
    if es.indices.exists(index=INDEX):
        es.indices.delete(index=INDEX)
    mapping = {
        "mappings": {
            "properties": {
                "name": {"type": "text"},
                "price": {"type": "float"},
                "description": {"type": "text"},
                "rating": {"type": "float"},
                "category": {"type": "keyword"},
                "availability": {"type": "keyword"},
                "image_url": {"type": "keyword"}
            }
        }
    }
    es.indices.create(index=INDEX, body=mapping)

def index_products(products):
    for i, product in enumerate(products):
        es.index(index=INDEX, id=str(i), body=product)

# --- Routes ---

@app.route("/api/crawl", methods=["POST"])
def crawl_and_index():
    try:
        products = asyncio.run(scrape_apniroots())
        create_index()
        index_products(products)
        return jsonify({"message": f"Indexed {len(products)} products"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/products", methods=["GET"])
def get_products():
    query = request.args.get("q", "")
    category = request.args.get("category")
    availability = request.args.get("availability")
    min_price = request.args.get("min_price")
    max_price = request.args.get("max_price")
    skip = int(request.args.get("skip", 0))
    limit = int(request.args.get("limit", 10))

    es_query = {"bool": {"must": [], "filter": []}}
    es_query["bool"]["must"].append({"match": {"name": query}}) if query else es_query["bool"]["must"].append({"match_all": {}})
    if category:
        es_query["bool"]["filter"].append({"term": {"category.keyword": category}})
    if availability:
        es_query["bool"]["filter"].append({"term": {"availability.keyword": availability}})
    if min_price or max_price:
        range_filter = {}
        if min_price:
            try:
                range_filter["gte"] = float(min_price)
            except ValueError:
                return jsonify({"error": "min_price must be a number"}), 400
        if max_price:
            try:
                range_filter["lte"] = float(max_price)
            except ValueError:
                return jsonify({"error": "max_price must be a number"}), 400
        es_query["bool"]["filter"].append({"range": {"price": range_filter}})

    try:
        res = es.search(index=INDEX, body={"query": es_query}, from_=skip, size=limit)

        # Include _id in each product
        hits = [
            {
                "id": hit["_id"],
                **hit["_source"]
            }
            for hit in res["hits"]["hits"]
        ]

        return jsonify(hits)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/products/<product_id>", methods=["GET"])
def get_product_by_id(product_id):
    print("Requested ID:", product_id)
    try:
        res = es.get(index=INDEX, id=product_id)
        product = {
            "id": res["_id"],
            **res["_source"]
        }
        return jsonify(product)
    except exceptions.NotFoundError:
        return jsonify({"error": "Product not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/ids", methods=["GET"])
def debug_ids():
    res = es.search(index=INDEX, body={"query": {"match_all": {}}}, size=1000)
    return jsonify([hit["_id"] for hit in res["hits"]["hits"]])



@app.route("/")
def health():
    return "✅ Unified Crawler & Search API is running."

if __name__ == "__main__":
    try:
        if es.ping():
            print("Connected to Elasticsearch successfully.")
        else:
            raise ValueError("Elasticsearch not reachable.")
    except Exception as e:
        print(f"Startup ES connection failed: {e}")
        exit(1)

    app.run(debug=True)
