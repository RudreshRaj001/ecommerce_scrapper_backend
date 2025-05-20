from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
import asyncio
import json
import re
import os
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# --- MongoDB Setup ---
MONGO_URI = "mongodb+srv://rjrudi7:dQqS!t_Q68NZpWU@cluster0.wjkfk6h.mongodb.net/"
MONGO_DB = "product_db"
MONGO_COLLECTION = "products"

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
collection = db[MONGO_COLLECTION]

# --- Flask Setup ---
app = Flask(__name__)
CORS(app)

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

# --- Routes ---

@app.route("/api/crawl", methods=["POST"])
def crawl_and_store():
    try:
        products = asyncio.run(scrape_apniroots())
        collection.delete_many({})
        collection.insert_many(products)
        return jsonify({"message": f"Inserted {len(products)} products"}), 200
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

    mongo_query = {}
    if query:
        mongo_query["name"] = {"$regex": query, "$options": "i"}
    if category:
        mongo_query["category"] = category
    if availability:
        mongo_query["availability"] = availability
    if min_price or max_price:
        price_filter = {}
        if min_price:
            price_filter["$gte"] = float(min_price)
        if max_price:
            price_filter["$lte"] = float(max_price)
        mongo_query["price"] = price_filter

    try:
        results = list(collection.find(mongo_query).skip(skip).limit(limit))
        for doc in results:
            doc["id"] = str(doc["_id"])
            del doc["_id"]
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/products/<product_id>", methods=["GET"])
def get_product_by_id(product_id):
    try:
        doc = collection.find_one({"_id": ObjectId(product_id)})
        if doc:
            doc["id"] = str(doc["_id"])
            del doc["_id"]
            return jsonify(doc)
        return jsonify({"error": "Product not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/ids", methods=["GET"])
def debug_ids():
    try:
        ids = [str(doc["_id"]) for doc in collection.find({}, {"_id": 1})]
        return jsonify(ids)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def health():
    return "✅ Unified Crawler & MongoDB API is running."


if __name__ == "__main__":
    print("✅ Connected to MongoDB successfully.")
    app.run(debug=True)
