# bot.py
import logging
import json
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.error import BadRequest
from config import (
    TELEGRAM_TOKEN, ADMIN_IDS, PAYMENT_QR_FILE, PAYMENT_UPI_ID,
    DB_PATH, TOL_MM
)
from services import (
    init_db, get_phone, check_compat, find_compatible_glasses, normalize_glass,
    list_devices_by_brand, update_phone_dimensions, get_plans, add_payment,
    get_payment, list_payments, update_payment_status, check_batch_compatibility,
    find_devices_by_dimensions, add_device_suggestion, format_compatible_devices,
    get_user_subscription_status, add_phone, device_exists,
    get_subscription_details, increment_query_count, check_query_limit,
    add_compatible_devices, get_compatible_devices, update_device_from_source,
    escape_markdown_v2
)
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)

# System prompt for Open AI
SYSTEM_PROMPT = """
You are a Telegram bot assistant that helps users and admins manage device compatibility and subscriptions. Interpret the user's message in any language and identify the intent and parameters. Respond with a JSON object containing:
- "intent": One of "check_compatibility", "list_compatible", "find_by_dimensions", "batch_compatibility", "suggest_device", "buy_subscription", "view_subscription", "view_compatible_devices", "list_devices", "add_device", "edit_device", "add_compatible_devices", "review_suggestions", "fetch_device", "cancel", or "unknown".
- "parameters": A dictionary of relevant parameters (e.g., device models, dimensions).
- "response": A natural language response to send to the user (optional, for direct replies).

Available intents and parameters:
1. check_compatibility: Check if two devices are compatible.
   - Parameters: model1 (str), model2 (str), htol (float, optional), wtol (float, optional), dtol (float, optional).
   - Example: "Check if Samsung Galaxy S21 guard fits iPhone 13, htol=2, wtol=2, dtol=0.1"
2. list_compatible: List devices compatible with a given model based on dimensions and verified data.
   - Parameters: model (str).
   - Example: "List devices compatible with Galaxy S21"
3. find_by_dimensions: Find devices by dimension ranges (requires Pro plan).
   - Parameters: height_min (float), height_max (float), width_min (float), width_max (float), diagonal_min (float), diagonal_max (float).
   - Example: "Find devices with height 150-155, width 70-75, diagonal 6.0-6.2"
4. batch_compatibility: Check compatibility for multiple devices (requires Pro plan).
   - Parameters: devices (list of str).
   - Example: "Check compatibility for Samsung Galaxy S21, iPhone 13, Pixel 6"
5. suggest_device: Suggest a new device for review.
   - Parameters: brand (str), model (str), height_mm (float), width_mm (float), diagonal_in (float), notch_type (str).
   - Example: "Suggest Samsung Galaxy S21, 150.0 mm height, 70.0 mm width, 6.1 in diagonal, Punch-hole notch"
6. buy_subscription: Initiate subscription purchase.
   - Parameters: plan_id (str, optional).
   - Example: "Buy a Pro plan"
7. view_subscription: View user's subscription details (plan and validity period).
   - Parameters: none.
   - Example: "Show my subscription details"
8. view_compatible_devices: View verified and dimension-based compatible devices for a model (requires Pro plan).
   - Parameters: model (str).
   - Example: "Show verified compatible devices for Galaxy S21"
9. list_devices: List devices for a brand (admin only).
   - Parameters: brand (str).
   - Example: "List Samsung devices"
10. add_device: Add a new device directly to the glasses table (admin only).
    - Parameters: brand (str), model (str), height_mm (float), width_mm (float), diagonal_in (float), notch_type (str).
    - Example: "Add Samsung Galaxy S21, 150.0 mm height, 70.0 mm width, 6.1 in diagonal, Punch-hole notch"
11. edit_device: Edit a device’s dimensions and notch type (admin only).
    - Parameters: brand (str), model (str), height_mm (float), width_mm (float), diagonal_in (float), notch_type (str).
    - Example: "Edit Samsung Galaxy S21 to 151.0 mm height, 71.0 mm width, 6.2 in diagonal, Waterdrop notch"
12. add_compatible_devices: Add verified compatible devices for a model (admin only).
    - Parameters: brand (str), model (str), compatible_devices (list of [brand, model] pairs).
    - Example: "Add compatible devices for Samsung Galaxy S21: Samsung Galaxy S20, iPhone 12"
13. review_suggestions: Review pending device suggestions (admin only).
    - Parameters: none.
    - Example: "Review device suggestions"
14. fetch_device: Fetch a device’s specs from GSMArena and add/update it (admin only).
    - Parameters: brand (str), model (str).
    - Example: "Fetch device Samsung Galaxy S25"
15. cancel: Cancel the current action (e.g., payment process).
    - Parameters: none.
    - Example: "Cancel"
16. unknown: For unrecognized intents.
    - Parameters: none.
    - Example: "Hello, how are you?"

Free plan users are limited to 10 queries per day; Pro plan users have unlimited queries and access to verified compatible devices. For multi-step processes (e.g., buy_subscription requiring a screenshot), indicate the next step in the response and maintain context. Admin users can use natural language for admin-specific intents (list_devices, add_device, edit_device, add_compatible_devices, review_suggestions, fetch_device).
"""

async def send_markdown_v2(update, text, reply_markup=None):
    try:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
    except BadRequest as e:
        logging.error(f"MarkdownV2 parsing error: {e}")
        plain_text = text.replace('\\', '')
        await update.message.reply_text(
            plain_text,
            reply_markup=reply_markup
        )

async def notify_admins(context, error_message, user_id=None):
    message = f"🚨 *Error Notification*\nUser ID: {user_id or 'Unknown'}\nError: {escape_markdown_v2(str(error_message))}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")

async def error_handler(update, context):
    user_id = update.effective_user.id if update.effective_user else None
    error_message = str(context.error)
    logging.exception("Error:")
    await notify_admins(context, error_message, user_id)
    await update.message.reply_text(
        escape_markdown_v2("Something went wrong. Please try again or contact support."),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "👋 Welcome! I'm an AI-powered bot that helps you check device compatibility and manage subscriptions. "
        "Just tell me what you want in natural language, like:\n"
        "- 'Check if Samsung Galaxy S21 fits iPhone 13'\n"
        "- 'List compatible devices for Galaxy S21'\n"
        "- 'Buy a Pro plan'\n"
        "Admins can manage devices with commands like 'Add device Samsung Galaxy S25' or 'Fetch device Samsung Galaxy S25'.\n"
        "You're on the Free Plan by default (10 queries/day). Use natural language, and I'll understand!"
    )
    await send_markdown_v2(update, message)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()

    # Handle payment screenshot state
    if context.user_data.get("state") == "awaiting_payment_screenshot":
        if user_message.lower() in ["cancel", "stop", "exit", "/cancel"]:
            context.user_data.pop("state", None)
            context.user_data.pop("buy_plan_id", None)
            await send_markdown_v2(update, "✅ Payment process cancelled")
            return
        await send_markdown_v2(update, "❌ Please send a photo screenshot of your payment or type /cancel to exit")
        return

    # Check query limit for non-pro users (excluding admins)
    if user_id not in ADMIN_IDS and get_user_subscription_status(user_id) != "pro":
        query_count = check_query_limit(user_id)
        if query_count >= 10:
            message = (
                "❌ You've reached the daily query limit of 10 for the free plan.\n"
                "Upgrade to the Pro plan for unlimited queries and access to verified compatible devices!"
            )
            await send_markdown_v2(update, message)
            return

    # Process text input with Open AI
    try:
        openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as e:
        error_message = f"Open AI API error: {e}"
        logging.error(error_message)
        await notify_admins(context, error_message, user_id)
        await send_markdown_v2(update, "Sorry, I couldn't process your request. Please try again.")
        return

    intent = result.get("intent", "unknown")
    parameters = result.get("parameters", {})
    response_text = result.get("response", "")

    # Increment query count for relevant intents (non-admins only)
    if user_id not in ADMIN_IDS and intent in [
        "check_compatibility", "list_compatible", "find_by_dimensions",
        "batch_compatibility", "view_compatible_devices"
    ]:
        increment_query_count(user_id)

    # Handle intents
    if intent == "check_compatibility":
        await handle_check_compatibility(update, context, parameters)
    elif intent in ["list_compatible", "view_compatible_devices"]:
        await handle_list_compatible(update, context, parameters)
    elif intent == "find_by_dimensions":
        if user_id not in ADMIN_IDS and get_user_subscription_status(user_id) != "pro":
            await send_markdown_v2(update, "⚠️ This feature requires a Pro plan")
            return
        await handle_find_by_dimensions(update, context, parameters)
    elif intent == "batch_compatibility":
        if user_id not in ADMIN_IDS and get_user_subscription_status(user_id) != "pro":
            await send_markdown_v2(update, "⚠️ This feature requires a Pro plan")
            return
        await handle_batch_compatibility(update, context, parameters)
    elif intent == "suggest_device":
        await handle_suggest_device(update, context, parameters)
    elif intent == "buy_subscription":
        await handle_buy_subscription(update, context, parameters)
    elif intent == "view_subscription":
        await handle_view_subscription(update, context)
    elif intent in ["list_devices", "add_device", "edit_device", "add_compatible_devices", "review_suggestions", "fetch_device"]:
        if user_id not in ADMIN_IDS:
            await send_markdown_v2(update, "❌ Unauthorized")
            return
        if intent == "list_devices":
            await handle_list_devices(update, context, parameters)
        elif intent == "add_device":
            await handle_add_device(update, context, parameters)
        elif intent == "edit_device":
            await handle_edit_device(update, context, parameters)
        elif intent == "add_compatible_devices":
            await handle_add_compatible_devices(update, context, parameters)
        elif intent == "review_suggestions":
            await handle_review_suggestions(update, context)
        elif intent == "fetch_device":
            await handle_fetch_device(update, context, parameters)
    elif intent == "cancel":
        await cancel(update, context)
    else:
        message = escape_markdown_v2(response_text) or "❓ I didn't understand your request. Try a command like 'Check if Samsung Galaxy S21 fits iPhone 13'."
        await send_markdown_v2(update, message)

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get("state") == "awaiting_payment_screenshot":
        photos = update.message.photo
        if not photos:
            await send_markdown_v2(update, "❌ Please send a valid photo screenshot of your payment")
            return
        await process_payment_screenshot(update, context)
    else:
        await send_markdown_v2(update, "❓ I didn't expect a photo. Please type a command like 'Buy a Pro plan'.")

async def handle_check_compatibility(update: Update, context, params):
    user_id = update.effective_user.id
    model1 = params.get("model1")
    model2 = params.get("model2")
    htol = float(params.get("htol", TOL_MM))
    wtol = float(params.get("wtol", TOL_MM))
    dtol = float(params.get("dtol", 0.1))

    if not model1 or not model2:
        await send_markdown_v2(update, "❌ Please specify two models, e.g., 'Samsung Galaxy S21, iPhone 13'")
        return

    p1 = get_phone(model1) or get_phone(normalize_glass(model1) or "")
    p2 = get_phone(model2) or get_phone(normalize_glass(model2) or "")
    if not p1 or not p2:
        await send_markdown_v2(update, "❌ Model(s) not found")
        return

    fit = check_compat(p1, p2, htol, wtol, dtol)
    message = (
        f"🛡️ *{escape_markdown_v2(f'{p1[0]} {p1[1]}')}* guard {'fits' if fit else 'does NOT fit'} *{escape_markdown_v2(f'{p2[0]} {p2[1]}')}*\n"
        f"Sizes: {p1[2]}×{p1[3]} mm, {p1[4]} in, Notch: {escape_markdown_v2(p1[5])} vs {p2[2]}×{p2[3]} mm, {p2[4]} in, Notch: {escape_markdown_v2(p2[5])}\n\n"
        f"⚠️ *Note*: Dimensions-based precision may vary. Pro plan users get access to a verified compatible devices list for higher accuracy"
    )
    await send_markdown_v2(update, message)

async def handle_list_compatible(update: Update, context, params):
    user_id = update.effective_user.id
    model = params.get("model")
    is_pro = get_user_subscription_status(user_id) == "pro"

    if not model:
        await send_markdown_v2(update, "❌ Please specify a model, e.g., 'Galaxy S21'")
        return

    corr = normalize_glass(model)
    if not corr:
        await send_markdown_v2(update, f"❌ Could not recognize “{escape_markdown_v2(model)}”")
        return

    arr = find_compatible_glasses(corr)
    if not arr:
        await send_markdown_v2(
            update,
            f"⚠️ No compatible devices found for “{escape_markdown_v2(corr)}”\n\n"
            f"⚠️ *Note*: Verified devices are admin-confirmed with exact matches. Dimension-based results use ±{TOL_MM}mm tolerance and may vary"
        )
        return

    note = (
        "✅ *Verified* devices are admin-confirmed with exact matches. "
        "*Dimension-based* results use ±{TOL_MM}mm tolerance and may vary"
    ) if is_pro else (
        "⚠️ *Note*: Dimensions-based precision may vary. "
        "Pro plan users get access to a verified compatible devices list for higher accuracy"
    )
    await send_markdown_v2(update, format_compatible_devices(arr) + "\n\n" + note)

async def handle_find_by_dimensions(update: Update, context, params):
    user_id = update.effective_user.id
    try:
        height_min = float(params.get("height_min"))
        height_max = float(params.get("height_max"))
        width_min = float(params.get("width_min"))
        width_max = float(params.get("width_max"))
        diagonal_min = float(params.get("diagonal_min"))
        diagonal_max = float(params.get("diagonal_max"))
    except (KeyError, ValueError):
        await send_markdown_v2(update, "❌ Please specify dimension ranges, e.g., 'height 150-155, width 70-75, diagonal 6.0-6.2'")
        return

    arr = find_devices_by_dimensions(height_min, height_max, width_min, width_max, diagonal_min, diagonal_max)
    if not arr:
        await send_markdown_v2(
            update,
            "⚠️ No devices found in this range\n\n"
            f"⚠️ *Note*: Dimensions-based precision may vary. Pro plan users get access to a verified compatible devices list for higher accuracy"
        )
        return

    formatted_arr = [(b, m, h, w, d, nt, 'Dimension-based') for b, m, h, w, d, nt in arr]
    await send_markdown_v2(
        update,
        format_compatible_devices(formatted_arr) + "\n\n"
        f"⚠️ *Note*: Dimensions-based precision may vary. Pro plan users get access to a verified compatible devices list for higher accuracy"
    )

async def handle_batch_compatibility(update: Update, context, params):
    user_id = update.effective_user.id
    devices = params.get("devices", [])
    if len(devices) < 2:
        await send_markdown_v2(update, "❌ Please provide at least 2 devices, e.g., 'Samsung Galaxy S21, iPhone 13'")
        return

    results = check_batch_compatibility(devices)
    if not results:
        await send_markdown_v2(
            update,
            "❌ No valid devices found\n\n"
            f"⚠️ *Note*: Dimensions-based precision may vary. Pro plan users get access to a verified compatible devices list for higher accuracy"
        )
        return

    lines = [
        f"{escape_markdown_v2(f'{b1} {m1}')} ↔ {escape_markdown_v2(f'{b2} {m2}')}: {'Compatible' if fit else 'Not Compatible'}"
        for (b1, m1), (b2, m2), fit in results
    ]
    message = (
        "\n".join(lines) + "\n\n"
        f"⚠️ *Note*: Dimensions-based precision may vary. Pro plan users get access to a verified compatible devices list for higher accuracy"
    )
    await send_markdown_v2(update, message)

async def handle_suggest_device(update: Update, context, params):
    user_id = update.effective_user.id
    try:
        brand = params.get("brand")
        model = params.get("model")
        height_mm = float(params.get("height_mm"))
        width_mm = float(params.get("width_mm"))
        diagonal_in = float(params.get("diagonal_in"))
        notch_type = params.get("notch_type", "None")
    except (KeyError, ValueError):
        await send_markdown_v2(update, "❌ Please specify device details, e.g., 'Samsung, Galaxy S21, 150.0 mm height, 70.0 mm width, 6.1 in diagonal, Punch-hole notch'")
        return

    try:
        add_device_suggestion(user_id, brand, model, height_mm, width_mm, diagonal_in, notch_type)
        await send_markdown_v2(update, "✅ Suggestion submitted for review")
        for admin in ADMIN_IDS:
            message = (
                f"🔔 New suggestion: {escape_markdown_v2(brand)} {escape_markdown_v2(model)} "
                f"({height_mm}×{width_mm} mm, {diagonal_in} in, Notch: {escape_markdown_v2(notch_type)})"
            )
            await context.bot.send_message(
                admin,
                message,
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        await notify_admins(context, f"Error processing device suggestion: {e}", user_id)
        await send_markdown_v2(update, "❌ Failed to submit suggestion. Please try again.")

async def handle_buy_subscription(update: Update, context, parameters):
    user_id = update.effective_user.id
    plan_id = parameters.get("plan_id")

    plans = get_plans()
    if not plans:
        await send_markdown_v2(update, "❌ No plans available")
        return

    if not plan_id:
        lines = [
            f"{i+1}. *{escape_markdown_v2(pid)}* – ₹{price}\n{escape_markdown_v2(desc)}"
            for i, (pid, price, desc) in enumerate(plans)
        ]
        context.user_data["plans_list"] = plans
        message = (
            "📋 *Available Plans*:\n\n" + "\n\n".join(lines) +
            "\n\nPlease specify the plan number or ID, e.g., 'Buy plan 1' or 'Buy Pro plan'"
        )
        await send_markdown_v2(update, message)
        return

    idx = None
    if plan_id.isdigit():
        i = int(plan_id) - 1
        if 0 <= i < len(plans):
            idx = i
    else:
        for i, (pid, _, _) in enumerate(plans):
            if plan_id.lower() == pid.lower():
                idx = i
                break

    if idx is None:
        await send_markdown_v2(update, "❌ Invalid plan. Please specify the plan number or ID")
        return

    pid, price, _ = plans[idx]
    context.user_data["buy_plan_id"] = pid
    context.user_data["state"] = "awaiting_payment_screenshot"

    # Format caption and escape the entire string
    caption = (
        f"Please pay ₹{price} via UPI to {escape_markdown_v2(PAYMENT_UPI_ID)} for the *{escape_markdown_v2(pid)}* plan.\n"
        "Or scan the QR code below:"
    )
    caption = escape_markdown_v2(caption)  # Escape the entire caption
    logging.info(f"Attempting to send QR code with caption: {caption}")
    try:
        with open(PAYMENT_QR_FILE, 'rb') as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        await send_markdown_v2(update, "After paying, send a screenshot of the payment or type /cancel to exit")
    except FileNotFoundError as e:
        logging.error(f"QR code file not found: {PAYMENT_QR_FILE}")
        await send_markdown_v2(update, "❌ Payment QR code file not found. Please contact support.")
        await notify_admins(context, f"QR code file not found: {e}", user_id)
    except BadRequest as e:
        logging.error(f"Failed to send photo message: {e}")
        # Fallback to plain text caption
        try:
            with open(PAYMENT_QR_FILE, 'rb') as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=f"Please pay ₹{price} via UPI to {PAYMENT_UPI_ID} for the {pid} plan.\nOr scan the QR code below:"
                )
            await send_markdown_v2(update, "After paying, send a screenshot of the payment or type /cancel to exit")
        except Exception as fallback_e:
            logging.error(f"Fallback failed: {fallback_e}")
            await send_markdown_v2(update, "❌ Failed to send payment instructions. Please try again or contact support.")
            await notify_admins(context, f"Failed to send payment QR code (fallback): {fallback_e}", user_id)
    except Exception as e:
        logging.error(f"Unexpected error sending QR code: {e}")
        await send_markdown_v2(update, "❌ An unexpected error occurred. Please try again or contact support.")
        await notify_admins(context, f"Unexpected error sending QR code: {e}", user_id)

async def handle_view_subscription(update: Update, context):
    user_id = update.effective_user.id
    details = get_subscription_details(user_id)
    message = (
        f"📋 *Subscription Details*\n"
        f"Plan: {escape_markdown_v2(details['plan_id'])}\n"
        f"Activated: {escape_markdown_v2(details['created_at'])}\n"
        f"Valid Till: {escape_markdown_v2(details['valid_till'])}\n\n"
        f"{'Upgrade to the Pro plan for unlimited queries and exclusive features' if details['plan_id'] == 'free' else 'Enjoy your Pro plan benefits'}"
    )
    await send_markdown_v2(update, message)

async def process_payment_screenshot(update: Update, context):
    user_id = update.effective_user.id
    photos = update.message.photo
    if not photos:
        await send_markdown_v2(update, "❌ Please send a valid photo screenshot of your payment")
        return

    file_id = photos[-1].file_id
    plan_id = context.user_data.pop("buy_plan_id", None)
    context.user_data.pop("state", None)

    if not plan_id:
        await send_markdown_v2(update, "⚠️ No plan selected. Please start the payment process again")
        return

    try:
        pid = add_payment(user_id, plan_id, file_id)
        await send_markdown_v2(update, "✅ Payment received, pending approval")
        for admin in ADMIN_IDS:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{pid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{pid}")
            ]])
            caption = (
                f"🔔 New payment \\#{pid}\n"
                f"User: `{user_id}`\n"
                f"Plan: *{escape_markdown_v2(plan_id)}*"
            )
            await context.bot.send_photo(
                chat_id=admin,
                photo=file_id,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb
            )
    except Exception as e:
        logging.error(f"Error processing payment screenshot: {e}")
        await notify_admins(context, f"Error processing payment screenshot: {str(e)}", user_id)
        await send_markdown_v2(update, "❌ Failed to process payment. Please try again.")

async def handle_list_devices(update: Update, context, params):
    user_id = update.effective_user.id
    brand = params.get("brand")
    if not brand:
        await send_markdown_v2(update, "❌ Please specify a brand, e.g., 'Samsung'")
        return

    arr = list_devices_by_brand(brand)
    if not arr:
        await send_markdown_v2(update, f"⚠️ No devices for “{escape_markdown_v2(brand)}”")
        return

    lines = [
        f"{escape_markdown_v2(b)} {escape_markdown_v2(m)} – {h}×{w} mm, {d} in, Notch: {escape_markdown_v2(nt)}"
        for (b, m, h, w, d, nt) in arr
    ]
    await send_markdown_v2(update, "\n".join(lines))

async def handle_add_device(update: Update, context, params):
    user_id = update.effective_user.id
    try:
        brand = params.get("brand")
        model = params.get("model")
        height_mm = float(params.get("height_mm"))
        width_mm = float(params.get("width_mm"))
        diagonal_in = float(params.get("diagonal_in"))
        notch_type = params.get("notch_type", "None")
    except (KeyError, ValueError):
        await send_markdown_v2(update, "❌ Please specify device details, e.g., 'Samsung, Galaxy S21, 150.0 mm height, 70.0 mm width, 6.1 in diagonal, Punch-hole notch'")
        return

    if device_exists(brand, model):
        await send_markdown_v2(update, f"❌ Device {escape_markdown_v2(brand)} {escape_markdown_v2(model)} already exists in the database")
        return

    try:
        add_phone(f"{brand} {model}", height_mm, width_mm, diagonal_in, notch_type)
        await send_markdown_v2(update, f"✅ Added {escape_markdown_v2(brand)} {escape_markdown_v2(model)} to devices")
    except Exception as e:
        await notify_admins(context, f"Error adding device: {e}", user_id)
        await send_markdown_v2(update, "❌ Failed to add device. Please try again.")

async def handle_edit_device(update: Update, context, params):
    user_id = update.effective_user.id
    try:
        brand = params.get("brand")
        model = params.get("model")
        height_mm = float(params.get("height_mm"))
        width_mm = float(params.get("width_mm"))
        diagonal_in = float(params.get("diagonal_in"))
        notch_type = params.get("notch_type", "None")
    except (KeyError, ValueError):
        await send_markdown_v2(update, "❌ Please specify device details, e.g., 'Edit Samsung Galaxy S21 to 151.0 mm height, 71.0 mm width, 6.2 in diagonal, Waterdrop notch'")
        return

    if not device_exists(brand, model):
        await send_markdown_v2(update, f"❌ Device {escape_markdown_v2(brand)} {escape_markdown_v2(model)} not found in the database")
        return

    try:
        update_phone_dimensions(brand, model, height_mm, width_mm, diagonal_in, notch_type)
        await send_markdown_v2(update, f"✅ Updated {escape_markdown_v2(brand)} {escape_markdown_v2(model)}")
    except Exception as e:
        await notify_admins(context, f"Error editing device: {e}", user_id)
        await send_markdown_v2(update, "❌ Failed to update device. Please try again.")

async def handle_add_compatible_devices(update: Update, context, params):
    user_id = update.effective_user.id
    try:
        brand = params.get("brand")
        model = params.get("model")
        compatible_devices = params.get("compatible_devices", [])
    except (KeyError, ValueError):
        await send_markdown_v2(update, "❌ Please specify device and compatible devices, e.g., 'Add compatible devices for Samsung Galaxy S21: Samsung Galaxy S20, iPhone 12'")
        return

    if not device_exists(brand, model):
        await send_markdown_v2(update, f"❌ Device {escape_markdown_v2(brand)} {escape_markdown_v2(model)} not found in the database")
        return

    valid_devices = []
    for compat_brand, compat_model in compatible_devices:
        if device_exists(compat_brand, compat_model):
            valid_devices.append((compat_brand, compat_model))
        else:
            await send_markdown_v2(update, f"⚠️ Compatible device {escape_markdown_v2(compat_brand)} {escape_markdown_v2(compat_model)} not found in the database. Skipping")

    if not valid_devices:
        await send_markdown_v2(update, "❌ No valid compatible devices provided")
        return

    try:
        add_compatible_devices(brand, model, valid_devices)
        await send_markdown_v2(update, f"✅ Added {len(valid_devices)} compatible devices for {escape_markdown_v2(brand)} {escape_markdown_v2(model)}")
    except Exception as e:
        await notify_admins(context, f"Error adding compatible devices: {e}", user_id)
        await send_markdown_v2(update, "❌ Failed to add compatible devices. Please try again.")

async def handle_review_suggestions(update: Update, context):
    user_id = update.effective_user.id
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id,brand,model,height_mm,width_mm,diagonal_in,notch_type FROM device_suggestions WHERE status='pending'"
        ).fetchall()
    if not rows:
        await send_markdown_v2(update, "⚠️ No pending suggestions")
        return

    for rid, b, m, h, w, d, nt in rows:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_sug_{rid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_sug_{rid}")
        ]])
        message = (
            f"Suggestion #{rid}: {escape_markdown_v2(b)} {escape_markdown_v2(m)} "
            f"({h}×{w} mm, {d} in, Notch: {escape_markdown_v2(nt)})"
        )
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb
        )
    await send_markdown_v2(update, "Select a suggestion to approve or reject")

async def handle_fetch_device(update: Update, context, params):
    user_id = update.effective_user.id
    brand = params.get("brand")
    model = params.get("model")
    if not brand or not model:
        await send_markdown_v2(update, "❌ Please provide brand and model, e.g., 'Samsung Galaxy S25'")
        return

    try:
        status, message = update_device_from_source(brand, model)
        formatted_message = f"✅ {escape_markdown_v2(message)}" if status else f"❌ {escape_markdown_v2(message)}"
        await send_markdown_v2(update, formatted_message)
    except Exception as e:
        await notify_admins(context, f"Error fetching device: {e}", user_id)
        await send_markdown_v2(update, "❌ Failed to fetch device. Please try again.")

async def payment_review_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    action, sid = query.data.split("_", 1)
    pid = int(sid)
    rec = get_payment(pid)
    if not rec:
        await query.edit_message_caption(
            caption=escape_markdown_v2("❌ Payment record missing"),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None
        )
        return
    _, uid, plan_id, _, _, _ = rec
    new_status = "approved" if action == "approve" else "rejected"
    try:
        update_payment_status(pid, new_status)
        caption = escape_markdown_v2(f"✅ Payment #{pid} *{new_status}*")
        await query.edit_message_caption(
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None
        )
        message = f"Your payment for plan *{escape_markdown_v2(plan_id)}* has been *{escape_markdown_v2(new_status)}*"
        await context.bot.send_message(
            chat_id=uid,
            text=message,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        await notify_admins(context, f"Error reviewing payment: {e}", uid)
        await query.edit_message_caption(
            caption=escape_markdown_v2("❌ Failed to process payment review"),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None
        )

async def suggestion_review_callback(update: Update, context):
    query = update.callback_query
    await query.answer()
    action, sid = query.data.split("_", 1)
    rid = int(sid.split("_")[-1])
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT brand,model,height_mm,width_mm,diagonal_in,notch_type FROM device_suggestions WHERE id=?",
            (rid,)
        ).fetchone()
        if not row:
            await query.edit_message_text(
                escape_markdown_v2("❌ Suggestion not found"),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        b, m, h, w, d, nt = row

        if action == "approve" and device_exists(b, m):
            conn.execute(
                "UPDATE device_suggestions SET status='rejected' WHERE id=?",
                (rid,)
            )
            conn.commit()
            await query.edit_message_text(
                f"❌ Suggestion #{rid} rejected: {escape_markdown_v2(b)} {escape_markdown_v2(m)} already exists in the database",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        try:
            if action == "approve":
                add_phone(f"{b} {m}", h, w, d, nt)
                conn.execute(
                    "UPDATE device_suggestions SET status='approved' WHERE id=?",
                    (rid,)
                )
            else:
                conn.execute(
                    "UPDATE device_suggestions SET status='rejected' WHERE id=?",
                    (rid,)
                )
            conn.commit()
            await query.edit_message_text(
                escape_markdown_v2(f"✅ Suggestion #{rid} {action}d"),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            await notify_admins(context, f"Error reviewing suggestion: {e}", update.effective_user.id)
            await query.edit_message_text(
                escape_markdown_v2("❌ Failed to process suggestion review"),
                parse_mode=ParseMode.MARKDOWN_V2
            )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data.pop("state", None)
    context.user_data.pop("buy_plan_id", None)
    await send_markdown_v2(update, "👋 Action cancelled")

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(CallbackQueryHandler(payment_review_callback, pattern=r'^(approve|reject)_\d+$'))
    app.add_handler(CallbackQueryHandler(suggestion_review_callback, pattern=r'^(approve_sug|reject_sug)_\d+$'))
    app.add_handler(CommandHandler("cancel", cancel))

    app.run_polling()

if __name__ == "__main__":
    main()