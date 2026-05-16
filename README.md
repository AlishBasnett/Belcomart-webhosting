# Belco Mart Website - PostgreSQL Render Version

Professional Flask and PostgreSQL website for Belco Mart, a South Asian grocery store in Belconnen, Canberra.

## Important database note

This copy is prepared for Render with PostgreSQL. It reads the database connection from `DATABASE_URL`.

The app creates any missing PostgreSQL tables and the `product_stock_view` / `order_summary_view` views with `CREATE TABLE IF NOT EXISTS` and `CREATE OR REPLACE VIEW`. Existing tables are reused.

## Setup

1. Create and activate a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Update `.env` with your PostgreSQL connection string.

```env
DATABASE_URL=postgresql://user:password@host:5432/database
SECRET_KEY=change_this_secret_key
```

4. Make sure PostgreSQL is reachable from your machine or from Render.

5. Run the Flask app.

```bash
python app.py
```

6. Open the website.

[http://127.0.0.1:5000](http://127.0.0.1:5000)

## Admin login

Admin access is available at:

[http://127.0.0.1:5000/admin/login](http://127.0.0.1:5000/admin/login)

The route only allows a user from the existing `users` table with:

- `email = admin@belcomart.com`
- `role = admin`

The app supports Werkzeug password hashes for future users. If your current sample admin password is plain text, it will still work temporarily so you can log in and update the stored password to a hash later.

To generate a secure password hash:

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password-here'))"
```

Then update the admin user's password column in MySQL Workbench with that generated hash.

## Product images

Product image paths are read from the existing product row using `image_url` or `image_path` if those columns exist. Admin product forms can upload an image into `static/images/products/` or store an image path/URL.

Only use images that you own or that are free/open-source/licensed for your use.

## Render deployment

Use these settings:

```text
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app
```

Set these environment variables in Render:

```env
DATABASE_URL=your-render-postgresql-connection-string
SECRET_KEY=replace-with-a-long-random-secret
```
