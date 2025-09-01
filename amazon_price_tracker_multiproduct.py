import requests
from bs4 import BeautifulSoup
from datetime import datetime
import csv
import os
import schedule
import time
import smtplib
from email.message import EmailMessage
import getpass
import json
from urllib.parse import urlparse
import re

# == CONFIG ==
CSV_FILE = "amazon_price_history.csv"
PRODUCTS_FILE = "tracked_products.json"
CSV_HEADERS = ["Timestamp", "Product Name", "Price (USD)", "Product URL", "Product ID"]
DEFAULT_CHECK_TIME = "09:00"    # Default check time

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# == GLOBALS (set at runtime) ==
TRACKED_PRODUCTS = {}  # {product_id: {"url": url, "name": name, "last_price": price}}
EMAIL_ENABLED = False
EMAIL_CONFIG = {}
CURRENT_CHECK_TIME = DEFAULT_CHECK_TIME

# == UTILS ==
def generate_product_id(url):
    """Generate a unique product ID from URL"""
    parsed = urlparse(url)
    # Extract ASIN or product identifier from Amazon URL
    path_parts = parsed.path.split('/')
    for i, part in enumerate(path_parts):
        if part == 'dp' and i + 1 < len(path_parts):
            return path_parts[i + 1]
    # Fallback: use hash of URL
    return str(hash(url))[-8:]

def load_tracked_products():
    """Load tracked products from JSON file"""
    global TRACKED_PRODUCTS
    if os.path.exists(PRODUCTS_FILE):
        try:
            with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
                TRACKED_PRODUCTS = json.load(f)
            print(f"📂 Loaded {len(TRACKED_PRODUCTS)} tracked products")
        except Exception as e:
            print(f"❌ Error loading products file: {e}")
            TRACKED_PRODUCTS = {}
    else:
        TRACKED_PRODUCTS = {}

def save_tracked_products():
    """Save tracked products to JSON file"""
    try:
        with open(PRODUCTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(TRACKED_PRODUCTS, f, indent=2, ensure_ascii=False)
        print(f"💾 Products saved to JSON: {PRODUCTS_FILE}")
        return True
    except Exception as e:
        print(f"❌ Error saving products to JSON: {e}")
        return False

def save_to_csv(timestamp, name, price, url, product_id):
    """Save price data to CSV"""
    try:
        first_run = not os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if first_run:
                writer.writerow(CSV_HEADERS)
                print(f"📄 Created new CSV file: {CSV_FILE}")
            writer.writerow([timestamp, name, f"{price:.2f}", url, product_id])
        print(f"💾 Data saved to CSV: {CSV_FILE}")
        return True
    except Exception as e:
        print(f"❌ Error saving to CSV: {e}")
        return False

def get_last_logged_price(product_id):
    """Get the last logged price for a specific product"""
    if not os.path.isfile(CSV_FILE):
        return None
    
    try:
        with open(CSV_FILE, encoding="utf-8") as f:
            rows = list(csv.reader(f))
            if len(rows) <= 1:
                return None
            
            # Find the last entry for this product
            for row in reversed(rows[1:]):  # Skip header
                if len(row) >= 5 and row[4] == product_id:
                    return float(row[2])
        return None
    except Exception as e:
        print(f"❌ Error reading CSV for product {product_id}: {e}")
        return None

def send_email_alert(product_name, current_price, previous_price, url, timestamp):
    """Send email alert for price change"""
    if previous_price is None:
        subject = f"🆕 New Product Tracked: {product_name} - ${current_price:.2f}"
        change_text = "Now tracking this product!"
    elif current_price < previous_price:
        subject = f"📉 Price Drop: {product_name} - ${current_price:.2f}"
        savings = previous_price - current_price
        change_text = f"Price dropped by ${savings:.2f} (was ${previous_price:.2f})"
    else:
        subject = f"📈 Price Increase: {product_name} - ${current_price:.2f}"
        increase = current_price - previous_price
        change_text = f"Price increased by ${increase:.2f} (was ${previous_price:.2f})"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_CONFIG["username"]
    msg["To"] = EMAIL_CONFIG["recipient"]
    
    content = f"""
Product: {product_name}
Current Price: ${current_price:.2f}
{change_text}
URL: {url}
Checked at: {timestamp}
    """.strip()
    
    msg.set_content(content)

    try:
        with smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"]) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_CONFIG["username"], EMAIL_CONFIG["password"])
            smtp.send_message(msg)
        print("✉️  Email alert sent.")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")

def fetch_product_details(url):
    """Fetch product name and price from Amazon URL"""
    try:
        r = requests.get(url, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        title_el = soup.find(id="productTitle")
        price_el = soup.find("span", class_="a-offscreen")

        if not title_el:
            print(f"❌ Could not find product title for URL: {url}")
            return None, None

        if not price_el:
            # Try alternative price selectors
            price_selectors = [
                "span.a-price-whole",
                ".a-price .a-offscreen",
                "#price_inside_buybox",
                ".a-price-range .a-offscreen"
            ]
            
            for selector in price_selectors:
                price_el = soup.select_one(selector)
                if price_el:
                    break
            
            if not price_el:
                print(f"❌ Could not find price for URL: {url}")
                return None, None

        name = title_el.get_text(strip=True)
        price_text = price_el.get_text(strip=True).replace("$", "").replace(",", "")
        
        # Handle price ranges (take the first price)
        if "to" in price_text.lower() or "-" in price_text:
            price_text = price_text.split()[0].replace("-", "")
        
        try:
            price = float(price_text)
        except ValueError:
            print(f"❌ Could not parse price '{price_text}' for URL: {url}")
            return None, None

        return name, price

    except requests.RequestException as e:
        print(f"❌ Network error fetching URL {url}: {e}")
        return None, None
    except Exception as e:
        print(f"❌ Error parsing product data for URL {url}: {e}")
        return None, None

def check_all_products():
    """Check prices for all tracked products"""
    if not TRACKED_PRODUCTS:
        print("❌ No products to check. Add some products first.")
        return 0
    
    print(f"\n🔍 Checking {len(TRACKED_PRODUCTS)} products...")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changes_detected = 0
    products_updated = False
    
    for product_id, product_data in TRACKED_PRODUCTS.items():
        url = product_data["url"]
        current_name = product_data.get('name', 'Unknown Product')
        print(f"\n📦 Checking: {current_name}")
        
        name, current_price = fetch_product_details(url)
        
        if name is None or current_price is None:
            print(f"⏭️  Skipping product {product_id} due to fetch error")
            continue
        
        # Update product name if it changed or was unknown
        if product_data.get("name") != name:
            print(f"📝 Product name updated: {name}")
            TRACKED_PRODUCTS[product_id]["name"] = name
            products_updated = True
        
        # Get last logged price from CSV (more reliable than JSON)
        last_logged_price = get_last_logged_price(product_id)
        
        # Check if price changed
        if last_logged_price is None or abs(current_price - last_logged_price) > 0.01:  # Use small tolerance for float comparison
            if last_logged_price is None:
                print(f"💲 First price check: ${current_price:.2f}")
            else:
                change = current_price - last_logged_price
                change_symbol = "📈" if change > 0 else "📉"
                print(f"💲 Price changed {change_symbol}: ${current_price:.2f} (was ${last_logged_price:.2f}, change: ${change:+.2f})")
            
            # Save to CSV automatically
            csv_saved = save_to_csv(timestamp, name, current_price, url, product_id)
            
            # Update in-memory data
            TRACKED_PRODUCTS[product_id]["last_price"] = current_price
            products_updated = True
            
            # Send email if enabled
            if EMAIL_ENABLED:
                try:
                    send_email_alert(name, current_price, last_logged_price, url, timestamp)
                except Exception as e:
                    print(f"❌ Email sending failed: {e}")
            
            if csv_saved:
                changes_detected += 1
            
        else:
            print(f"⏸️  No change (still ${current_price:.2f})")
        
        # Small delay between requests to be respectful
        time.sleep(2)
    
    # Save updated product data to JSON if any changes occurred
    if products_updated:
        json_saved = save_tracked_products()
        if json_saved:
            print(f"✅ All changes saved successfully!")
        else:
            print(f"⚠️  Some changes may not have been saved to JSON.")
    
    print(f"\n🎯 Price check completed: {changes_detected} price changes detected and saved.")
    
    return changes_detected

def add_product(url):
    """Add a new product to track"""
    product_id = generate_product_id(url)
    
    if product_id in TRACKED_PRODUCTS:
        print(f"⚠️  Product already being tracked (ID: {product_id})")
        return False
    
    print(f"🔍 Fetching product details...")
    name, price = fetch_product_details(url)
    
    if name is None or price is None:
        print("❌ Could not fetch product details. Product not added.")
        return False
    
    # Add to tracked products
    TRACKED_PRODUCTS[product_id] = {
        "url": url,
        "name": name,
        "last_price": price
    }
    
    print(f"✅ Added: {name} (${price:.2f})")
    
    # Save initial price to CSV
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    csv_saved = save_to_csv(timestamp, name, price, url, product_id)
    
    # Save updated products to JSON
    json_saved = save_tracked_products()
    
    # Send email notification for new product
    if EMAIL_ENABLED:
        try:
            send_email_alert(name, price, None, url, timestamp)
        except Exception as e:
            print(f"❌ Email sending failed: {e}")
    
    if csv_saved and json_saved:
        print("✅ Product successfully added and saved to both CSV and JSON!")
        return True
    else:
        print("⚠️  Product added but there were saving issues.")
        return False

def remove_product():
    """Remove a product from tracking"""
    if not TRACKED_PRODUCTS:
        print("❌ No products are currently being tracked.")
        return
    
    print("\n📋 Currently tracked products:")
    for i, (product_id, data) in enumerate(TRACKED_PRODUCTS.items(), 1):
        print(f"{i}. {data.get('name', 'Unknown')} (ID: {product_id})")
    
    try:
        choice = int(input("\nEnter the number of the product to remove (0 to cancel): "))
        if choice == 0:
            return
        
        product_ids = list(TRACKED_PRODUCTS.keys())
        if 1 <= choice <= len(product_ids):
            product_id = product_ids[choice - 1]
            product_name = TRACKED_PRODUCTS[product_id].get('name', 'Unknown')
            del TRACKED_PRODUCTS[product_id]
            
            # Save updated products to JSON
            if save_tracked_products():
                print(f"✅ Removed: {product_name} (saved to JSON)")
            else:
                print(f"⚠️  Removed: {product_name} (but JSON save failed)")
        else:
            print("❌ Invalid selection.")
    except ValueError:
        print("❌ Invalid input.")

def list_products():
    """List all tracked products"""
    if not TRACKED_PRODUCTS:
        print("❌ No products are currently being tracked.")
        return
    
    print(f"\n📋 Tracked Products ({len(TRACKED_PRODUCTS)}):")
    print("-" * 80)
    
    for product_id, data in TRACKED_PRODUCTS.items():
        name = data.get('name', 'Unknown Product')
        price = data.get('last_price', 'N/A')
        url = data.get('url', '')
        
        print(f"ID: {product_id}")
        print(f"Name: {name}")
        print(f"Last Price: ${price:.2f}" if isinstance(price, (int, float)) else f"Last Price: {price}")
        print(f"URL: {url}")
        print("-" * 80)

def validate_time_format(time_str):
    """Validate time format (HH:MM)"""
    pattern = r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'
    return bool(re.match(pattern, time_str))

def get_custom_check_time():
    """Get custom check time from user"""
    global CURRENT_CHECK_TIME
    
    print("\n⏰ Schedule Configuration")
    print("Current scheduled time:", CURRENT_CHECK_TIME)
    print("Examples: 09:00, 14:30, 22:15")
    
    while True:
        new_time = input("Enter your preferred check time (HH:MM) or press Enter to use current: ").strip()
        
        if not new_time:  # User pressed Enter, use current time
            print(f"✅ Using current time: {CURRENT_CHECK_TIME}")
            return CURRENT_CHECK_TIME
        
        if validate_time_format(new_time):
            CURRENT_CHECK_TIME = new_time
            print(f"✅ Schedule time set to: {CURRENT_CHECK_TIME}")
            
            # Save the schedule time to config
            save_schedule_config()
            return CURRENT_CHECK_TIME
        else:
            print("❌ Invalid time format. Please use HH:MM format (e.g., 09:00, 14:30)")

def save_schedule_config():
    """Save schedule configuration to file"""
    try:
        config = {
            "check_time": CURRENT_CHECK_TIME,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        with open("schedule_config.json", "w") as f:
            json.dump(config, f, indent=2)
        print(f"💾 Schedule configuration saved.")
    except Exception as e:
        print(f"⚠️  Could not save schedule config: {e}")

def load_schedule_config():
    """Load schedule configuration from file"""
    global CURRENT_CHECK_TIME
    
    if os.path.exists("schedule_config.json"):
        try:
            with open("schedule_config.json", "r") as f:
                config = json.load(f)
            CURRENT_CHECK_TIME = config.get("check_time", DEFAULT_CHECK_TIME)
            print(f"📅 Loaded schedule: Daily at {CURRENT_CHECK_TIME}")
        except Exception as e:
            print(f"⚠️  Could not load schedule config: {e}")
            CURRENT_CHECK_TIME = DEFAULT_CHECK_TIME

# == SCHEDULER ==
def schedule_daily_run():
    """Schedule daily price checks with custom time"""
    if not TRACKED_PRODUCTS:
        print("❌ No products to monitor. Add some products first.")
        return
    
    # Get custom time from user
    check_time = get_custom_check_time()
    
    print(f"\n⏰ Scheduled daily check at {check_time}")
    print(f"📦 Monitoring {len(TRACKED_PRODUCTS)} products")
    print("Press Ctrl+C to stop the scheduler")
    print("-" * 50)
    
    # Clear any existing schedules
    schedule.clear()
    
    # Schedule the job
    schedule.every().day.at(check_time).do(check_all_products)
    
    # Show next run time
    next_run = schedule.next_run()
    if next_run:
        print(f"🕐 Next check scheduled for: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
    except KeyboardInterrupt:
        print("\n\n👋 Scheduler stopped.")
        print("Your products are still tracked. Run the script again to resume monitoring.")

# == SETUP FUNCTIONS ==
def setup_email():
    """Setup email configuration"""
    global EMAIL_ENABLED, EMAIL_CONFIG
    
    choice = input("📧 Enable email alerts for price changes? (yes/no): ").strip().lower()
    
    if choice in ["yes", "y"]:
        EMAIL_ENABLED = True
        EMAIL_CONFIG = {
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "username": input("📨 Your Gmail address: ").strip(),
            "password": getpass.getpass("🔐 App password (not your main Gmail password): ").strip(),
            "recipient": input("📬 Recipient email: ").strip()
        }
        
        # Save email config to JSON for persistence
        config_data = {
            "email_enabled": EMAIL_ENABLED,
            "email_config": EMAIL_CONFIG
        }
        
        try:
            with open("email_config.json", "w") as f:
                json.dump(config_data, f, indent=2)
            print("✅ Email alerts enabled and configuration saved.")
        except Exception as e:
            print(f"✅ Email alerts enabled (config save failed: {e}).")
    else:
        EMAIL_ENABLED = False
        # Remove email config file if disabled
        if os.path.exists("email_config.json"):
            os.remove("email_config.json")
        print("❎ Email alerts disabled.")

def load_email_config():
    """Load email configuration from file"""
    global EMAIL_ENABLED, EMAIL_CONFIG
    
    if os.path.exists("email_config.json"):
        try:
            with open("email_config.json", "r") as f:
                config_data = json.load(f)
            
            EMAIL_ENABLED = config_data.get("email_enabled", False)
            EMAIL_CONFIG = config_data.get("email_config", {})
            
            if EMAIL_ENABLED:
                print("📧 Email alerts loaded and enabled.")
        except Exception as e:
            print(f"⚠️  Could not load email config: {e}")
            EMAIL_ENABLED = False
            EMAIL_CONFIG = {}

def interactive_menu():
    """Interactive menu for managing tracked products"""
    while True:
        print("\n" + "="*50)
        print("📦 Amazon Price Tracker - Multi-Product")
        print("="*50)
        print("1. Add product to track")
        print("2. Remove product")
        print("3. List tracked products")
        print("4. Check all prices now")
        print("5. Start scheduled monitoring")
        print("6. Configure email alerts")
        print("7. Change schedule time")
        print("8. Exit")
        print("-" * 50)
        
        # Show current settings
        print(f"📅 Current schedule: Daily at {CURRENT_CHECK_TIME}")
        print(f"📧 Email alerts: {'Enabled' if EMAIL_ENABLED else 'Disabled'}")
        print(f"📦 Tracked products: {len(TRACKED_PRODUCTS)}")
        print("-" * 50)
        
        choice = input("Select an option (1-8): ").strip()
        
        if choice == "1":
            url = input("\n🔗 Enter Amazon product URL: ").strip()
            if url.startswith("http") and "amazon." in url:
                print("🔄 Adding product...")
                success = add_product(url)
                if success:
                    print("🎉 Product successfully added and all data saved!")
                else:
                    print("❌ Failed to add product or save data.")
            else:
                print("❌ Invalid Amazon URL.")
        
        elif choice == "2":
            remove_product()
        
        elif choice == "3":
            list_products()
        
        elif choice == "4":
            if TRACKED_PRODUCTS:
                print("🔄 Checking all products for price changes...")
                changes = check_all_products()
                if changes > 0:
                    print(f"🎉 {changes} price changes found and saved!")
                else:
                    print("ℹ️  No price changes detected.")
            else:
                print("❌ No products to check. Add some products first.")
        
        elif choice == "5":
            schedule_daily_run()
        
        elif choice == "6":
            setup_email()
        
        elif choice == "7":
            get_custom_check_time()
        
        elif choice == "8":
            print("👋 Goodbye!")
            break
        
        else:
            print("❌ Invalid option. Please try again.")

# == MAIN ==
if __name__ == "__main__":
    print("📦 Multi-Product Amazon Price Tracker")
    print("Loading existing data...")
    
    load_tracked_products()
    load_email_config()  # Load email settings
    load_schedule_config()  # Load schedule settings
    interactive_menu()