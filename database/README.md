# ADAPTIX demo database (Neon PostgreSQL)

This is a dedicated, isolated demo schema `adaptix_demo`, with a scratch schema `adaptix_demo_sandbox`. All data is **synthetic** and **deterministic**:

- 20 customers with fictional names and `@example.com` addresses;
- 15 products;
- 40 orders, with 79 order lines whose totals add up;
- 30 inventory events.

Experiments and audit records live in `adaptix_demo.adaptix_experiments` and `adaptix_demo.adaptix_audit_log`.

| File | Purpose |
|---|---|
| `seed_data.py` | the single source of the synthetic data and of the expected business results used for verification |
| `build_init_sql.py` | generates `init_demo.sql` (`python -m database.build_init_sql`; `--check` verifies it is current) |
| `init_demo.sql` | schemas, tables, constraints, indexes and seed data; safe to run more than once |
| `roles.sql` | optional least-privilege role `adaptix_app` that owns only the two demo schemas |

## Set it up on Neon

1. **Create a project** at https://neon.tech (the free plan is enough), then open **SQL Editor**.
2. **(Recommended) Create the least-privilege role.** Paste `roles.sql`, replace `<CHOOSE_A_STRONG_PASSWORD>` in the editor only, and run it.
3. **Create the tables and data.** Choose one:
   - **A, recommended:** do steps 4 and 5, then click **Set up demo database** on the app's *Live Project Integration* page. The app runs `init_demo.sql` as `adaptix_app`, so that role owns the tables and can run the demo faults.
   - **B:** paste `init_demo.sql` into the SQL Editor and run it. If you ran it as the project owner, also run the ownership snippet at the end of `roles.sql`.
4. **Get the connection string.** In Neon, open **Connect**, select role `adaptix_app`, and copy the string. It ends in `?sslmode=require`.
5. **Store it as a server-side secret** named `DATABASE_URL`. Never commit or share it.
   - Streamlit Community Cloud: open your app → **⋮ → Settings → Secrets**, add the line below, then **Save**:
     ```toml
     DATABASE_URL = "postgresql://adaptix_app:<password>@<host>/<database>?sslmode=require"
     ```
   - Locally: put the same line in `.streamlit/secrets.toml`, which is git-ignored.
6. **On the page:** click **Test connection**, then **Set up demo database**, then **Data**, where you should see 20 / 15 / 40 / 79 / 30 rows and `orders_with_consistent_totals: 40/40`.

## Reset

Use **Reset to baseline** on the page, which asks for confirmation. You can also re-run the `-- @section business` part of `init_demo.sql` in the SQL Editor. A reset recreates only the five business tables and keeps the experiment and audit logs.

## Safety

- **Isolation:** everything runs inside `adaptix_demo` and `adaptix_demo_sandbox`. Faults change only those schemas, and only after an explicit confirmation.
- **No free-form SQL:** the UI and API never accept SQL. Workflows use fixed SQL with identifiers passed through `psycopg.sql` and bound parameters.
- **Connection safety:** TLS is required for remote hosts. Statement timeout is 5 s, connect timeout 10 s, and idle-in-transaction timeout 30 s.
- **Secrets never leave the server:** the connection URL is never shown, logged or exported. Logs and exports are redacted.
