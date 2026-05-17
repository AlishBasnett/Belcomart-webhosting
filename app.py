import os
import uuid
from decimal import Decimal
from functools import wraps

import psycopg
from psycopg import Error, sql
from psycopg.rows import dict_row
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change_this_secret_key")
app.config["UPLOAD_FOLDER"] = os.path.join("static", "images", "products")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
DATABASE_URL = os.getenv("DATABASE_URL")
SCHEMA_READY = False

ORDER_STATUSES = [
    "pending",
    "accepted",
    "preparing",
    "ready_for_pickup",
    "out_for_delivery",
    "completed",
    "cancelled",
]

MESSAGE_STATUSES = ["new", "read", "replied"]
PRODUCT_STATUSES = ["available", "out_of_stock", "hidden"]
PAYMENT_METHODS = ["pay_in_store", "cash_on_delivery", "bank_transfer", "card"]
PRIMARY_KEYS = {
    "users": "user_id",
    "categories": "category_id",
    "products": "product_id",
    "cart_items": "cart_item_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "payments": "payment_id",
    "offers": "offer_id",
    "product_offers": "product_offer_id",
    "contact_messages": "message_id",
}


def get_db_connection():
    global SCHEMA_READY
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is required.")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    if not SCHEMA_READY:
        ensure_database_schema(conn)
        SCHEMA_READY = True
    return conn


def initialize_database():
    conn = get_db_connection()
    conn.close()


def quote_identifier(name):
    if name not in PRIMARY_KEYS:
        raise ValueError(f"Unsupported table: {name}")
    return sql.Identifier(name)


def quote_column(name):
    return sql.Identifier(name)


def query_db(query, params=None, one=False):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(query, params or ())
        rows = cursor.fetchall()
        return (rows[0] if rows else None) if one else rows
    except Error:
        app.logger.exception("Database query failed")
        return None if one else []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def execute_db(query, params=None, commit=True):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(query, params or ())
        returned = cursor.fetchone() if cursor.description else None
        if commit:
            conn.commit()
        return (next(iter(returned.values())) if returned else None), cursor.rowcount
    except Error:
        if conn:
            conn.rollback()
        app.logger.exception("Database write failed")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def table_columns(table_name):
    rows = query_db(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table_name,),
    )
    return {row["column_name"] for row in rows}


def allowed_columns(table_name, data):
    columns = table_columns(table_name)
    return {key: value for key, value in data.items() if key in columns}


def insert_row(table_name, data):
    clean = allowed_columns(table_name, data)
    if not clean:
        raise ValueError(f"No matching columns for {table_name}")
    primary_key = PRIMARY_KEYS.get(table_name, "id")
    query = sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING {primary_key}").format(
        table=quote_identifier(table_name),
        columns=sql.SQL(", ").join(map(quote_column, clean.keys())),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in clean),
        primary_key=quote_column(primary_key),
    )
    inserted_id, _ = execute_db(query, tuple(clean.values()))
    return inserted_id


def update_row(table_name, data, row_id):
    clean = allowed_columns(table_name, data)
    if not clean:
        return 0
    primary_key = PRIMARY_KEYS.get(table_name, "id")
    query = sql.SQL("UPDATE {table} SET {assignments} WHERE {primary_key} = %s").format(
        table=quote_identifier(table_name),
        assignments=sql.SQL(", ").join(
            sql.SQL("{} = {}").format(quote_column(key), sql.Placeholder()) for key in clean
        ),
        primary_key=quote_column(primary_key),
    )
    _, count = execute_db(query, tuple(clean.values()) + (row_id,))
    return count


def ensure_database_schema(conn):
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        user_id SERIAL PRIMARY KEY,
        full_name VARCHAR(150) NOT NULL,
        email VARCHAR(255) UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role VARCHAR(50) DEFAULT 'customer',
        status VARCHAR(50) DEFAULT 'active',
        phone VARCHAR(50),
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS categories (
        category_id SERIAL PRIMARY KEY,
        category_name VARCHAR(150) NOT NULL,
        description TEXT,
        image_path TEXT,
        status VARCHAR(50) DEFAULT 'active',
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS products (
        product_id SERIAL PRIMARY KEY,
        category_id INTEGER REFERENCES categories(category_id) ON DELETE SET NULL,
        product_name VARCHAR(255) NOT NULL,
        brand VARCHAR(150),
        description TEXT,
        origin_country VARCHAR(100),
        price NUMERIC(10, 2) NOT NULL DEFAULT 0,
        sale_price NUMERIC(10, 2),
        stock_quantity INTEGER DEFAULT 0,
        unit VARCHAR(50) DEFAULT 'each',
        status VARCHAR(50) DEFAULT 'available',
        is_featured BOOLEAN DEFAULT false,
        image_path TEXT,
        image_url TEXT,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS cart_items (
        cart_item_id SERIAL PRIMARY KEY,
        session_id VARCHAR(100) NOT NULL,
        product_id INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
        quantity INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS orders (
        order_id SERIAL PRIMARY KEY,
        customer_name VARCHAR(150) NOT NULL,
        customer_phone VARCHAR(50) NOT NULL,
        customer_email VARCHAR(255),
        order_type VARCHAR(50) DEFAULT 'pickup',
        delivery_address TEXT,
        payment_method VARCHAR(50),
        payment_status VARCHAR(50) DEFAULT 'unpaid',
        order_status VARCHAR(50) DEFAULT 'pending',
        subtotal NUMERIC(10, 2) DEFAULT 0,
        delivery_fee NUMERIC(10, 2) DEFAULT 0,
        discount_amount NUMERIC(10, 2) DEFAULT 0,
        total_amount NUMERIC(10, 2) DEFAULT 0,
        order_notes TEXT,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS order_items (
        order_item_id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
        product_id INTEGER REFERENCES products(product_id) ON DELETE SET NULL,
        product_name VARCHAR(255),
        quantity INTEGER NOT NULL DEFAULT 1,
        unit_price NUMERIC(10, 2) DEFAULT 0,
        subtotal NUMERIC(10, 2) DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS payments (
        payment_id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
        payment_method VARCHAR(50),
        amount NUMERIC(10, 2) DEFAULT 0,
        payment_status VARCHAR(50) DEFAULT 'pending',
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS offers (
        offer_id SERIAL PRIMARY KEY,
        offer_title VARCHAR(255) NOT NULL,
        description TEXT,
        discount_type VARCHAR(50),
        discount_value NUMERIC(10, 2),
        status VARCHAR(50) DEFAULT 'active',
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS product_offers (
        product_offer_id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
        offer_id INTEGER NOT NULL REFERENCES offers(offer_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS contact_messages (
        message_id SERIAL PRIMARY KEY,
        full_name VARCHAR(150) NOT NULL,
        email VARCHAR(255),
        phone VARCHAR(50),
        subject VARCHAR(255),
        message TEXT NOT NULL,
        status VARCHAR(50) DEFAULT 'new',
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE OR REPLACE VIEW product_stock_view AS
    SELECT p.*, c.category_name
    FROM products p
    LEFT JOIN categories c ON c.category_id = p.category_id;

    CREATE OR REPLACE VIEW order_summary_view AS
    SELECT o.*, COUNT(oi.order_item_id) AS item_count
    FROM orders o
    LEFT JOIN order_items oi ON oi.order_id = o.order_id
    GROUP BY o.order_id;
    """
    with conn.cursor() as cursor:
        cursor.execute(schema)
    seed_database_if_empty(conn)
    conn.commit()


def seed_database_if_empty(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS count FROM categories")
        categories_empty = cursor.fetchone()["count"] == 0
        cursor.execute("SELECT COUNT(*) AS count FROM products")
        products_empty = cursor.fetchone()["count"] == 0

        if categories_empty or products_empty:
            cursor.execute(
                """
                INSERT INTO categories (category_name, description, image_path, status)
                SELECT category_name, description, image_path, status
                FROM (
                    VALUES
                        ('Rice & Grains', 'Basmati rice, atta, flours, grains, and everyday pantry staples.', NULL, 'active'),
                        ('Spices', 'Whole and ground spices for South Asian cooking.', NULL, 'active'),
                        ('Snacks', 'Namkeen, biscuits, sweets, and quick bites.', NULL, 'active'),
                        ('Lentils', 'Dals, beans, chickpeas, and pulses.', NULL, 'active'),
                        ('Frozen Food', 'Frozen breads, vegetables, snacks, and ready-to-cook favourites.', NULL, 'active'),
                        ('Drinks', 'Tea, juices, soft drinks, and traditional beverages.', NULL, 'active'),
                        ('Household', 'Kitchen, cleaning, prayer, and daily household essentials.', NULL, 'active')
                ) AS seed_categories(category_name, description, image_path, status)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM categories
                    WHERE categories.category_name = seed_categories.category_name
                )
                """
            )

        if products_empty:
            cursor.execute(
                """
                WITH seed_products AS (
                    SELECT *
                    FROM (
                        VALUES
                            ('Rice & Grains', 'Premium Basmati Rice 5kg', 'India Gate', 'Long-grain aromatic basmati rice for biryani and everyday meals.', 'India', 18.99, 16.99, 30, '5kg bag', 'available', true, NULL),
                            ('Rice & Grains', 'Sona Masoori Rice 5kg', 'Lal Qilla', 'Medium-grain rice for daily cooking, curd rice, and light meals.', 'India', 15.99, NULL, 24, '5kg bag', 'available', false, NULL),
                            ('Rice & Grains', 'Whole Wheat Atta 10kg', 'Aashirvaad', 'Whole wheat flour for soft rotis, chapatis, and parathas.', 'India', 22.99, 20.99, 18, '10kg bag', 'available', true, NULL),
                            ('Rice & Grains', 'Besan Gram Flour 1kg', 'Pattu', 'Fine chickpea flour for pakoras, kadhi, sweets, and batters.', 'Australia', 5.49, NULL, 40, '1kg pack', 'available', false, NULL),
                            ('Spices', 'Turmeric Powder 200g', 'MDH', 'Bright haldi powder for curries, dals, marinades, and rice dishes.', 'India', 3.49, NULL, 45, '200g pack', 'available', true, NULL),
                            ('Spices', 'Garam Masala 100g', 'Everest', 'Aromatic spice blend for curries, gravies, and finishing dishes.', 'India', 3.99, NULL, 36, '100g pack', 'available', false, NULL),
                            ('Spices', 'Cumin Seeds 200g', 'Shan', 'Whole jeera seeds for tempering, tadka, and spice mixes.', 'Pakistan', 4.49, NULL, 32, '200g pack', 'available', false, NULL),
                            ('Spices', 'Kashmiri Chilli Powder 200g', 'TRS', 'Mild chilli powder with deep red colour for curries and marinades.', 'India', 4.99, 4.49, 25, '200g pack', 'available', true, NULL),
                            ('Spices', 'Coriander Powder 200g', 'MDH', 'Ground dhania powder for everyday South Asian cooking.', 'India', 3.79, NULL, 30, '200g pack', 'available', false, NULL),
                            ('Snacks', 'Aloo Bhujia 400g', 'Haldiram''s', 'Classic crispy potato and gram flour namkeen.', 'India', 4.99, NULL, 50, '400g pack', 'available', true, NULL),
                            ('Snacks', 'Masala Peanuts 200g', 'Haldiram''s', 'Crunchy peanuts coated with spicy masala.', 'India', 3.49, NULL, 42, '200g pack', 'available', false, NULL),
                            ('Snacks', 'Parle-G Biscuits 800g', 'Parle', 'Popular glucose biscuits for tea time and lunch boxes.', 'India', 5.99, NULL, 35, '800g pack', 'available', false, NULL),
                            ('Snacks', 'Soan Papdi 500g', 'Bikano', 'Flaky traditional Indian sweet for sharing and gifting.', 'India', 6.49, 5.99, 20, '500g box', 'available', true, NULL),
                            ('Lentils', 'Toor Dal 2kg', 'Pattu', 'Split pigeon peas for classic dal, sambar, and khichdi.', 'Australia', 8.99, NULL, 28, '2kg pack', 'available', true, NULL),
                            ('Lentils', 'Moong Dal 1kg', 'Pattu', 'Split yellow mung beans for light dals and soups.', 'Australia', 5.99, NULL, 34, '1kg pack', 'available', false, NULL),
                            ('Lentils', 'Red Lentils Masoor Dal 1kg', 'TRS', 'Quick-cooking red lentils for soups, curries, and stews.', 'India', 4.99, NULL, 38, '1kg pack', 'available', false, NULL),
                            ('Lentils', 'Kabuli Chana 1kg', 'Pattu', 'Dried white chickpeas for chole, salads, and curries.', 'Australia', 5.49, NULL, 26, '1kg pack', 'available', false, NULL),
                            ('Lentils', 'Urad Dal 1kg', 'TRS', 'Black gram dal for dosa batter, dal makhani, and papad recipes.', 'India', 5.79, NULL, 22, '1kg pack', 'available', false, NULL),
                            ('Frozen Food', 'Plain Paratha 20 Pack', 'Kawan', 'Frozen layered flatbread, ready to heat on a pan.', 'Malaysia', 9.99, 8.99, 22, '20 pack', 'available', true, NULL),
                            ('Frozen Food', 'Vegetable Samosa 12 Pack', 'Deep', 'Crispy pastry filled with spiced vegetables.', 'India', 7.99, NULL, 18, '12 pack', 'available', false, NULL),
                            ('Frozen Food', 'Paneer 1kg', 'Gopi', 'Frozen paneer cubes for palak paneer, tikka, and curries.', 'Australia', 13.99, NULL, 16, '1kg pack', 'available', true, NULL),
                            ('Frozen Food', 'Garlic Naan 5 Pack', 'Haldiram''s', 'Soft frozen garlic naan ready to heat and serve.', 'India', 6.99, NULL, 20, '5 pack', 'available', false, NULL),
                            ('Drinks', 'Masala Chai Tea 500g', 'Wagh Bakri', 'Strong black tea blend for masala chai.', 'India', 7.49, NULL, 30, '500g pack', 'available', true, NULL),
                            ('Drinks', 'Mango Drink 1L', 'Maaza', 'Sweet mango fruit drink served chilled.', 'India', 3.49, NULL, 40, '1L bottle', 'available', false, NULL),
                            ('Drinks', 'Rose Syrup 750ml', 'Rooh Afza', 'Traditional rose-flavoured syrup for milk, water, and desserts.', 'Pakistan', 6.99, NULL, 24, '750ml bottle', 'available', false, NULL),
                            ('Drinks', 'Thums Up 300ml', 'Thums Up', 'Bold Indian cola-style soft drink.', 'India', 2.49, NULL, 36, '300ml bottle', 'available', false, NULL),
                            ('Household', 'Stainless Steel Masala Box', 'Generic', 'Round spice storage box with small stainless steel containers.', 'India', 19.99, NULL, 10, 'each', 'available', true, NULL),
                            ('Household', 'Incense Sticks Sandalwood', 'Cycle', 'Sandalwood fragrance incense sticks for home use.', 'India', 2.99, NULL, 50, 'pack', 'available', false, NULL),
                            ('Household', 'Pressure Cooker Gasket', 'Prestige', 'Replacement gasket for compatible pressure cookers.', 'India', 4.99, NULL, 14, 'each', 'available', false, NULL),
                            ('Household', 'Copper Pooja Diya', 'Generic', 'Small copper diya for prayer and festival use.', 'India', 3.99, NULL, 25, 'each', 'available', false, NULL)
                    ) AS products(category_name, product_name, brand, description, origin_country, price, sale_price, stock_quantity, unit, status, is_featured, image_path)
                )
                INSERT INTO products (
                    category_id, product_name, brand, description, origin_country, price,
                    sale_price, stock_quantity, unit, status, is_featured, image_path
                )
                SELECT
                    categories.category_id,
                    seed_products.product_name,
                    seed_products.brand,
                    seed_products.description,
                    seed_products.origin_country,
                    seed_products.price,
                    seed_products.sale_price,
                    seed_products.stock_quantity,
                    seed_products.unit,
                    seed_products.status,
                    seed_products.is_featured,
                    seed_products.image_path
                FROM seed_products
                JOIN categories ON categories.category_name = seed_products.category_name
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM products
                    WHERE products.product_name = seed_products.product_name
                )
                """
            )

    ensure_admin_user(conn)


def ensure_admin_user(conn):
    admin_email = "admin@belcomart.com"
    admin_password = os.getenv("ADMIN_PASSWORD", "BelcoAdmin@2026")
    password_hash = generate_password_hash(admin_password)

    with conn.cursor() as cursor:
        cursor.execute("SELECT user_id FROM users WHERE email = %s ORDER BY user_id LIMIT 1", (admin_email,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                """
                UPDATE users
                SET full_name = %s, password_hash = %s, role = %s, status = %s
                WHERE user_id = %s
                """,
                ("Belco Mart Admin", password_hash, "admin", "active", existing["user_id"]),
            )
        else:
            cursor.execute(
                """
                INSERT INTO users (full_name, email, password_hash, role, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                ("Belco Mart Admin", admin_email, password_hash, "admin", "active"),
            )


def get_cart_session_id():
    if "cart_session_id" not in session:
        session["cart_session_id"] = str(uuid.uuid4())
    return session["cart_session_id"]


def money(value):
    return Decimal(str(value or 0))


def current_price(product):
    return money(product.get("sale_price") or product.get("price"))


def product_image(product):
    image = (product or {}).get("image_url") or (product or {}).get("image_path")
    if image:
        if str(image).startswith(("http://", "https://", "/static/")):
            return image
        return url_for("static", filename=str(image).lstrip("/"))
    return "https://images.unsplash.com/photo-1542838132-92c53300491e?auto=format&fit=crop&w=900&q=80"


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            flash("Please log in to access admin.", "warning")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_globals():
    return {
        "store": {
            "name": "Belco Mart",
            "tagline": "Where Quality Meets Affordability",
            "phone": "0452 452 938",
            "location": "Belconnen, Canberra, ACT",
        },
        "product_image": product_image,
        "current_price": current_price,
        "cart_count": cart_count(),
    }


def scalar_count(query, params=None):
    row = query_db(query, params, one=True)
    return int(row.get("count", 0)) if row else 0


def cart_count():
    sid = session.get("cart_session_id")
    if not sid:
        return 0
    row = query_db(
        "SELECT COALESCE(SUM(quantity), 0) AS count FROM cart_items WHERE session_id = %s",
        (sid,),
        one=True,
    )
    return int(row["count"]) if row else 0


def cart_items():
    sid = get_cart_session_id()
    return query_db(
        """
        SELECT ci.cart_item_id AS cart_id, ci.quantity,
               p.product_id AS id, p.product_name AS name, p.*,
               c.category_name AS category_name
        FROM cart_items ci
        JOIN products p ON p.product_id = ci.product_id
        LEFT JOIN categories c ON c.category_id = p.category_id
        WHERE ci.session_id = %s
        ORDER BY ci.cart_item_id DESC
        """,
        (sid,),
    )


@app.before_request
def prepare_database():
    if request.endpoint != "healthz" and not SCHEMA_READY:
        initialize_database()


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/home")
def home():
    categories = query_db(
        """
        SELECT category_id AS id, category_name AS name, description, image_path, status
        FROM categories
        WHERE COALESCE(status, 'active') != 'inactive'
        ORDER BY category_name
        LIMIT 8
        """
    )
    featured = query_db(
        """
        SELECT p.product_id AS id, p.product_name AS name, p.*, c.category_name AS category_name
        FROM products p
        LEFT JOIN categories c ON c.category_id = p.category_id
        WHERE COALESCE(p.is_featured::text, 'false') IN ('1', 'true', 't', 'yes', 'on')
          AND COALESCE(p.status, 'available') != 'hidden'
        ORDER BY p.product_id DESC
        LIMIT 8
        """
    )
    offers = query_db(
        """
        SELECT offer_id AS id, offer_title AS title, description, discount_type, discount_value, status
        FROM offers
        WHERE COALESCE(status, 'active') = 'active'
        ORDER BY offer_id DESC
        LIMIT 4
        """
    )
    return render_template("index.html", categories=categories, featured=featured, offers=offers)


@app.route("/products")
def products():
    category_id = request.args.get("category", type=int)
    search = request.args.get("q", "").strip()
    categories = query_db("SELECT category_id AS id, category_name AS name, description FROM categories ORDER BY category_name")
    where = ["COALESCE(p.status, 'available') != 'hidden'"]
    params = []
    if category_id:
        where.append("p.category_id = %s")
        params.append(category_id)
    if search:
        where.append("(p.product_name ILIKE %s OR COALESCE(p.brand, '') ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    product_rows = query_db(
        f"""
        SELECT p.product_id AS id, p.product_name AS name, p.*, c.category_name AS category_name
        FROM products p
        LEFT JOIN categories c ON c.category_id = p.category_id
        WHERE {" AND ".join(where)}
        ORDER BY p.product_name
        """,
        tuple(params),
    )
    return render_template(
        "products.html",
        products=product_rows,
        categories=categories,
        active_category=category_id,
        search=search,
    )


@app.route("/products/<int:product_id>")
def product_detail(product_id):
    product = query_db(
        """
        SELECT p.product_id AS id, p.product_name AS name, p.*, c.category_name AS category_name
        FROM products p
        LEFT JOIN categories c ON c.category_id = p.category_id
        WHERE p.product_id = %s
        """,
        (product_id,),
        one=True,
    )
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("products"))
    return render_template("product_detail.html", product=product)


@app.post("/cart/add/<int:product_id>")
def add_to_cart(product_id):
    quantity = max(request.form.get("quantity", 1, type=int), 1)
    product = query_db(
        "SELECT product_id AS id, product_name AS name, products.* FROM products WHERE product_id = %s",
        (product_id,),
        one=True,
    )
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("products"))
    if product.get("status") == "out_of_stock" or int(product.get("stock_quantity") or 0) <= 0:
        flash("This product is currently out of stock.", "warning")
        return redirect(request.referrer or url_for("products"))

    sid = get_cart_session_id()
    existing = query_db(
        "SELECT * FROM cart_items WHERE session_id = %s AND product_id = %s",
        (sid, product_id),
        one=True,
    )
    if existing:
        execute_db(
            "UPDATE cart_items SET quantity = quantity + %s WHERE cart_item_id = %s",
            (quantity, existing["cart_item_id"]),
        )
    else:
        insert_row("cart_items", {"session_id": sid, "product_id": product_id, "quantity": quantity})
    flash("Added to cart.", "success")
    return redirect(request.referrer or url_for("cart"))


@app.route("/cart")
def cart():
    items = cart_items()
    subtotal = sum(current_price(item) * item["quantity"] for item in items)
    return render_template("cart.html", items=items, subtotal=subtotal)


@app.post("/cart/update/<int:cart_id>")
def update_cart(cart_id):
    quantity = request.form.get("quantity", 1, type=int)
    sid = get_cart_session_id()
    if quantity <= 0:
        execute_db("DELETE FROM cart_items WHERE cart_item_id = %s AND session_id = %s", (cart_id, sid))
    else:
        execute_db(
            "UPDATE cart_items SET quantity = %s WHERE cart_item_id = %s AND session_id = %s",
            (quantity, cart_id, sid),
        )
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:cart_id>")
def remove_cart_item(cart_id):
    execute_db("DELETE FROM cart_items WHERE cart_item_id = %s AND session_id = %s", (cart_id, get_cart_session_id()))
    flash("Item removed.", "info")
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items = cart_items()
    if not items:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("products"))

    subtotal = sum(current_price(item) * item["quantity"] for item in items)
    if request.method == "POST":
        name = request.form.get("customer_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        order_type = request.form.get("order_type", "pickup")
        address = request.form.get("delivery_address", "").strip()
        payment_method = request.form.get("payment_method", "pay_in_store")
        notes = request.form.get("order_notes", "").strip()

        if not name or not phone:
            flash("Name and phone are required.", "danger")
            return redirect(url_for("checkout"))
        if order_type == "delivery" and not address:
            flash("Delivery address is required for delivery orders.", "danger")
            return redirect(url_for("checkout"))
        if payment_method not in PAYMENT_METHODS:
            flash("Please choose a valid payment method.", "danger")
            return redirect(url_for("checkout"))

        delivery_fee = Decimal("5.00") if order_type == "delivery" else Decimal("0.00")
        discount = Decimal("0.00")
        total = subtotal + delivery_fee - discount
        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO orders (
                    customer_name, customer_phone, customer_email, order_type, delivery_address,
                    payment_method, payment_status, order_status, subtotal, delivery_fee,
                    discount_amount, total_amount, order_notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING order_id
                """,
                (
                    name,
                    phone,
                    email or None,
                    order_type,
                    address or None,
                    payment_method,
                    "unpaid",
                    "pending",
                    subtotal,
                    delivery_fee,
                    discount,
                    total,
                    notes or None,
                ),
            )
            order_id = cursor.fetchone()["order_id"]
            for item in items:
                cursor.execute(
                    """
                    INSERT INTO order_items (order_id, product_id, product_name, quantity, unit_price, subtotal)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        order_id,
                        item["id"],
                        item["name"],
                        item["quantity"],
                        current_price(item),
                        current_price(item) * item["quantity"],
                    ),
                )
            cursor.execute(
                """
                INSERT INTO payments (order_id, payment_method, amount, payment_status)
                VALUES (%s, %s, %s, %s)
                """,
                (order_id, payment_method, total, "pending"),
            )
            cursor.execute("DELETE FROM cart_items WHERE session_id = %s", (get_cart_session_id(),))
            conn.commit()
            return redirect(url_for("order_success", order_id=order_id))
        except Error:
            if conn:
                conn.rollback()
            app.logger.exception("Checkout failed")
            flash("We could not place your order right now. Please call the store.", "danger")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    delivery_fee = Decimal("0.00")
    return render_template("checkout.html", items=items, subtotal=subtotal, delivery_fee=delivery_fee)


@app.route("/order-success/<int:order_id>")
def order_success(order_id):
    order = query_db(
        "SELECT order_id AS id, order_status AS status, orders.* FROM orders WHERE order_id = %s",
        (order_id,),
        one=True,
    )
    return render_template("order_success.html", order=order)


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()
        if not name or not message:
            flash("Name and message are required.", "danger")
        else:
            try:
                insert_row(
                    "contact_messages",
                    {
                        "full_name": name,
                        "email": email,
                        "phone": phone,
                        "subject": subject,
                        "message": message,
                        "status": "new",
                    },
                )
                flash("Thanks, your message has been sent.", "success")
                return redirect(url_for("contact"))
            except Error:
                flash("Message could not be sent right now. Please call the store.", "danger")
    return render_template("contact.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = query_db(
            "SELECT user_id AS id, full_name AS name, password_hash AS password, users.* FROM users WHERE email = %s",
            (email,),
            one=True,
        )
        if user and password_matches(user.get("password"), password):
            session["user_id"] = user["id"]
            session["user_name"] = user.get("name") or email
            flash("Welcome back.", "success")
            return redirect(url_for("index"))
        flash("Invalid login details.", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if not name or not email or len(password) < 6:
            flash("Please enter your name, email and a password of at least 6 characters.", "danger")
        else:
            try:
                user_id = insert_row(
                        "users",
                        {
                        "full_name": name,
                        "email": email,
                        "password_hash": generate_password_hash(password),
                        "role": "customer",
                        "status": "active",
                    },
                )
                session["user_id"] = user_id
                session["user_name"] = name
                flash("Account created.", "success")
                return redirect(url_for("index"))
            except Error:
                flash("Could not create account. The email may already be registered.", "danger")
    return render_template("register.html")


def password_matches(stored_password, raw_password):
    if not stored_password:
        return False
    if str(stored_password).startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored_password, raw_password)
    return stored_password == raw_password


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if email == "admin@belcomart.com":
            conn = None
            try:
                conn = get_db_connection()
                ensure_admin_user(conn)
                conn.commit()
            except Error:
                if conn:
                    conn.rollback()
                app.logger.exception("Admin user setup failed")
            finally:
                if conn:
                    conn.close()
        user = query_db(
            "SELECT user_id AS id, full_name AS name, password_hash AS password, users.* FROM users WHERE email = %s AND role = 'admin'",
            (email,),
            one=True,
        )
        if email == "admin@belcomart.com" and user and password_matches(user.get("password"), password):
            session["admin_id"] = user["id"]
            session["admin_email"] = email
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin login.", "danger")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    session.pop("admin_email", None)
    flash("Logged out.", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin")
@require_admin
def admin_dashboard():
    stats = {
        "products": scalar_count("SELECT COUNT(*) AS count FROM products"),
        "categories": scalar_count("SELECT COUNT(*) AS count FROM categories"),
        "orders": scalar_count("SELECT COUNT(*) AS count FROM orders"),
        "pending": scalar_count("SELECT COUNT(*) AS count FROM orders WHERE order_status = 'pending'"),
        "low_stock": scalar_count("SELECT COUNT(*) AS count FROM product_stock_view WHERE stock_quantity <= 5"),
    }
    recent_orders = query_db(
        "SELECT order_id AS id, order_status AS status, order_summary_view.* FROM order_summary_view ORDER BY order_id DESC LIMIT 6"
    )
    recent_messages = query_db(
        "SELECT message_id AS id, full_name AS name, contact_messages.* FROM contact_messages ORDER BY message_id DESC LIMIT 5"
    )
    return render_template("admin_dashboard.html", stats=stats, recent_orders=recent_orders, recent_messages=recent_messages)


@app.route("/admin/products")
@require_admin
def admin_products():
    product_rows = query_db(
        """
        SELECT p.product_id AS id, p.product_name AS name, p.*, c.category_name AS category_name
        FROM products p
        LEFT JOIN categories c ON c.category_id = p.category_id
        ORDER BY p.product_id DESC
        """
    )
    return render_template("admin_products.html", products=product_rows)


def product_form_payload():
    image_path = request.form.get("image_path", "").strip()
    uploaded = request.files.get("image_file")
    if uploaded and uploaded.filename:
        filename = secure_filename(uploaded.filename)
        absolute = os.path.join(app.root_path, app.config["UPLOAD_FOLDER"], filename)
        uploaded.save(absolute)
        image_path = f"images/products/{filename}"
    return {
        "product_name": request.form.get("name", "").strip(),
        "brand": request.form.get("brand", "").strip(),
        "description": request.form.get("description", "").strip(),
        "origin_country": request.form.get("origin_country", "").strip(),
        "price": request.form.get("price") or 0,
        "sale_price": request.form.get("sale_price") or None,
        "stock_quantity": request.form.get("stock_quantity") or 0,
        "unit": request.form.get("unit", "").strip(),
        "category_id": request.form.get("category_id") or None,
        "status": request.form.get("status", "available"),
        "is_featured": bool(request.form.get("is_featured")),
        "image_path": image_path,
    }


@app.route("/admin/products/add", methods=["GET", "POST"])
@require_admin
def admin_add_product():
    categories = query_db("SELECT category_id AS id, category_name AS name FROM categories ORDER BY category_name")
    if request.method == "POST":
        payload = product_form_payload()
        if not payload["product_name"] or not payload["price"]:
            flash("Product name and price are required.", "danger")
        else:
            insert_row("products", payload)
            flash("Product added.", "success")
            return redirect(url_for("admin_products"))
    return render_template("admin_add_product.html", categories=categories, product=None, statuses=PRODUCT_STATUSES)


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@require_admin
def admin_edit_product(product_id):
    product = query_db(
        "SELECT product_id AS id, product_name AS name, products.* FROM products WHERE product_id = %s",
        (product_id,),
        one=True,
    )
    categories = query_db("SELECT category_id AS id, category_name AS name FROM categories ORDER BY category_name")
    if not product:
        flash("Product not found.", "danger")
        return redirect(url_for("admin_products"))
    if request.method == "POST":
        payload = product_form_payload()
        update_row("products", payload, product_id)
        flash("Product updated.", "success")
        return redirect(url_for("admin_products"))
    return render_template("admin_edit_product.html", product=product, categories=categories, statuses=PRODUCT_STATUSES)


@app.post("/admin/products/<int:product_id>/status")
@require_admin
def admin_product_status(product_id):
    status = request.form.get("status", "available")
    if status not in PRODUCT_STATUSES:
        status = "available"
    update_row("products", {"status": status}, product_id)
    flash("Product status updated.", "success")
    return redirect(url_for("admin_products"))


@app.route("/admin/orders")
@require_admin
def admin_orders():
    orders = query_db(
        "SELECT order_id AS id, order_status AS status, order_summary_view.* FROM order_summary_view ORDER BY order_id DESC"
    )
    return render_template("admin_orders.html", orders=orders, statuses=ORDER_STATUSES)


@app.route("/admin/orders/<int:order_id>", methods=["GET", "POST"])
@require_admin
def admin_order_detail(order_id):
    if request.method == "POST":
        status = request.form.get("status", "pending")
        if status in ORDER_STATUSES:
            update_row("orders", {"order_status": status}, order_id)
            flash("Order status updated.", "success")
        return redirect(url_for("admin_order_detail", order_id=order_id))
    order = query_db(
        "SELECT order_id AS id, order_status AS status, orders.* FROM orders WHERE order_id = %s",
        (order_id,),
        one=True,
    )
    items = query_db("SELECT * FROM order_items WHERE order_id = %s", (order_id,))
    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("admin_orders"))
    return render_template("admin_order_detail.html", order=order, items=items, statuses=ORDER_STATUSES)


@app.route("/admin/messages", methods=["GET", "POST"])
@require_admin
def admin_messages():
    if request.method == "POST":
        message_id = request.form.get("message_id", type=int)
        status = request.form.get("status", "read")
        if message_id and status in MESSAGE_STATUSES:
            update_row("contact_messages", {"status": status}, message_id)
            flash("Message status updated.", "success")
        return redirect(url_for("admin_messages"))
    messages = query_db(
        "SELECT message_id AS id, full_name AS name, contact_messages.* FROM contact_messages ORDER BY message_id DESC"
    )
    return render_template("admin_messages.html", messages=messages, statuses=MESSAGE_STATUSES)


@app.errorhandler(404)
def not_found(_error):
    return render_template("base.html", page_title="Page not found", simple_message="Page not found."), 404


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.template_filter("currency")
def currency_filter(value):
    return f"${money(value):,.2f}"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
