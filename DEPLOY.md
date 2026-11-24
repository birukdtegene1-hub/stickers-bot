# Deploy the order-flow update

Deploy the working source directory to the PythonAnywhere WSGI application. Install dependencies with the application's Python 3.10+ environment.

```sh
python -m pip install -r requirements.txt
```

## Database and artwork

1. Back up the current orders table. Apply `migrations/001_order_flow.sql` once using the Supabase SQL editor during the deployment window. Installations that already applied this migration can use manual payments immediately.
2. Give the server a Supabase service-role or secret key. The orders, artwork, and notification tables and functions are for the backend. The migration creates the private `sticker-artwork-private` bucket. Check existing Storage policies for broad public access to that bucket.
3. For existing orders, keep `SUPABASE_BUCKET` pointing at the original bucket. Make that bucket private if it was dedicated to this bot. Previously published artwork links stay publicly accessible until the bucket permissions change. Staff can open matching legacy originals through signed links from the print sheet. If the bucket is shared, move the bot's originals into a dedicated private bucket and migrate their recorded paths before retiring the public copies.
4. For older orders with outstanding payment links or an unknown deposit status, check your payment records before requesting money. After verifying receipt, open the order in Telegram and tap **Confirm deposit received**. This also works for older orders already in production.

## Settings

Keep `TELEGRAM_BOT_TOKEN`, `PA_DOMAIN`, `SUPABASE_URL`, and `SUPABASE_KEY` in the deployment environment or project `.env`. Set `ADMIN_TELEGRAM_IDS` to the comma-separated numeric Telegram user IDs of the staff who can confirm payments and manage fulfillment.

| Variable | Value |
|---|---|
| `TELEGRAM_WEBHOOK_SECRET` | New random Telegram webhook secret using letters, digits, underscores or hyphens. |
| `PICKUP_INSTRUCTIONS` | Actual pickup address and collection instructions. |
| `SPACEBAR_FIT`, `PHONE_BACK_FIT` | Printed dimensions and supported devices or key sizes. The defaults ask customers to confirm fit with the shop. |
| `ARTWORK_BUCKET` | Optional override for the new private bucket. Provision that bucket if overriding the default. |
| `WEBAPP_URL` | Optional HTTPS Mini App URL. The default is `https://<PA_DOMAIN>/webapp`. Route it to Flask so the form receives the current catalog and signed preview. |

If PythonAnywhere has a static-file mapping for `/static/webapp.html`, use the Flask `/webapp` URL for the keyboard. The HTML is now rendered by Flask.

## Webhooks and scheduled work

1. Reload the WSGI app. In the same environment and working directory, register Telegram once with `python -m flask --app app register-webhook`. Use this command again after changing the URL or Telegram secret.
2. Schedule `python -m flask --app app maintain` every five minutes using the deployment virtual environment and project directory. This retries pending notifications, removes expired abandoned artwork, and expires old Telegram update receipts. The immediate send and periodic retry use the same database queue. A crash immediately after Telegram accepts a notification can cause that message to be delivered again.
3. Check `/ready` after registration. It checks database access, the private bucket and Telegram's registered URL. `/health` is the process health endpoint.

## Acceptance check

Run `python -m unittest discover -s tests` locally. Start the bot and tap **Open sticker shop**. Choose 40 × 40 mm and set the estimate to five stickers. Expect 285.00 ETB with a 5% discount. Tap **Start an order**, attach the original artwork as a file in chat, then tap **Configure stickers**. Submit five 40 × 40 mm stickers for pickup using a +251 number. Expect a 300.00 ETB subtotal, 15.00 ETB discount, and 142.50 ETB each for the deposit and balance. In the staff chat, tap **Manage orders**, select the order and use the contact number to arrange payment. After collecting the deposit, tap **Confirm deposit received** and check the customer's notification. Start printing, mark the order ready, then confirm collection after receiving the balance. Tap **My orders** to verify the receipt. Repeat with delivery and verify dispatch and delivery messages.
