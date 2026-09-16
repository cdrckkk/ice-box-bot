import os
import sqlite3
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    CallbackQueryHandler, MessageHandler, filters, ContextTypes
)

# Note: nest_asyncio is not needed with python-telegram-bot v20.5+ Application API

# Database setup
DB_FILE = "ice_box_bookings.db"

# Admin User ID (set this to your Telegram ID for admin access)
ADMIN_USER_ID = 845290487  # Cedrick's admin ID

# Singapore is permanently UTC+8 (no daylight saving), so a fixed offset is
# safe and needs no timezone database. The server runs on UTC, so plain
# datetime.now() would be 8 hours behind the user and must not be used.
SGT = timezone(timedelta(hours=8))

def now_sgt():
    """Current time in Singapore, whatever timezone the server runs in."""
    return datetime.now(SGT)

def parse_booking_date(date_str, reference=None):
    """Turn a 'DD/MM' string into a real date, choosing the year closest to
    `reference`. Comparing these as plain text is wrong across a year
    boundary: '01/10' < '30/09' is lexicographically true, which would treat
    October bookings as already expired during September."""
    reference = reference or now_sgt()
    try:
        day, month = (int(part) for part in date_str.split("/"))
    except (ValueError, AttributeError):
        return None
    best = None
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidate = datetime(year, month, day, tzinfo=SGT).date()
        except ValueError:
            continue  # e.g. 29/02 in a non-leap year
        gap = abs((candidate - reference.date()).days)
        if best is None or gap < best[0]:
            best = (gap, candidate)
    return best[1] if best else None

def init_db():
    """Initialize database with tables."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                ice_box_id INTEGER,
                start_time TEXT,
                end_time TEXT,
                booking_date TEXT,
                return_photo_id TEXT,
                return_time TEXT,
                is_returned BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error initializing database: {e}")

def get_ice_boxes():
    """Return list of ice boxes."""
    return ['A', 'B', 'C', 'D', 'E', 'F', 'G']

def check_conflict(ice_box_id, start_time, end_time, booking_date, exclude_booking_id=None):
    """Check if time slot is available."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        query = '''
            SELECT * FROM bookings
            WHERE ice_box_id = ? AND booking_date = ?
            AND NOT (end_time <= ? OR start_time >= ?)
        '''
        params = [ice_box_id, booking_date, start_time, end_time]

        if exclude_booking_id:
            query += " AND id != ?"
            params.append(exclude_booking_id)

        cursor.execute(query, params)
        result = cursor.fetchone()
        conn.close()

        return result is not None
    except Exception as e:
        print(f"Error checking conflict: {e}")
        return False

def get_available_boxes(start_time, end_time, booking_date):
    """Get available ice boxes for a given time slot."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        all_boxes = get_ice_boxes()

        cursor.execute('''
            SELECT DISTINCT ice_box_id FROM bookings
            WHERE booking_date = ?
            AND NOT (end_time <= ? OR start_time >= ?)
        ''', (booking_date, start_time, end_time))

        booked_boxes = [row[0] for row in cursor.fetchall()]
        conn.close()

        return [box for box in all_boxes if box not in booked_boxes]
    except Exception as e:
        print(f"Error getting available boxes: {e}")
        return get_ice_boxes()

def add_booking(user_id, user_name, ice_box_id, start_time, end_time, booking_date):
    """Add booking to database."""
    try:
        # Validate inputs
        if not all([user_id, user_name, ice_box_id, start_time, end_time, booking_date]):
            print(f"Invalid booking data: user_id={user_id}, user_name={user_name}, ice_box_id={ice_box_id}, start_time={start_time}, end_time={end_time}, booking_date={booking_date}")
            return None

        # Check for conflicts before inserting
        if check_conflict(ice_box_id, start_time, end_time, booking_date):
            print(f"Conflict detected for ice box {ice_box_id} on {booking_date} {start_time}-{end_time}")
            return None

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO bookings (user_id, user_name, ice_box_id, start_time, end_time, booking_date)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, user_name, ice_box_id, start_time, end_time, booking_date))

        conn.commit()
        booking_id = cursor.lastrowid
        conn.close()
        print(f"Booking created successfully: ID={booking_id}, user={user_name}, box={ice_box_id}")
        return booking_id
    except Exception as e:
        print(f"Error adding booking: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_user_bookings(user_id, include_returned=False):
    """Get a user's bookings. Returned ones are hidden by default: once the
    box is back and the photo is in, the booking is finished from the user's
    side. The row stays in the database so /admin_returns keeps its record."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        sql = '''
            SELECT id, ice_box_id, start_time, end_time, booking_date, is_returned, return_photo_id
            FROM bookings
            WHERE user_id = ?
        '''
        if not include_returned:
            sql += " AND is_returned = 0"
        sql += " ORDER BY booking_date DESC, start_time"
        cursor.execute(sql, (user_id,))

        bookings = cursor.fetchall()
        conn.close()
        return bookings
    except Exception as e:
        print(f"Error getting user bookings: {e}")
        return []

def get_all_bookings(include_returned=False):
    """Everyone's bookings. Returned ones are hidden by default so the shared
    list only shows boxes still out. /admin_returns passes include_returned=True
    because tracking returns is exactly its job."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        sql = '''
            SELECT id, user_name, ice_box_id, start_time, end_time, booking_date
            FROM bookings
        '''
        if not include_returned:
            sql += " WHERE is_returned = 0"
        sql += " ORDER BY booking_date, ice_box_id, start_time"
        cursor.execute(sql)

        bookings = cursor.fetchall()
        conn.close()
        return bookings
    except Exception as e:
        print(f"Error getting all bookings: {e}")
        return []

def delete_booking(booking_id):
    """Delete a booking."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM bookings WHERE id = ?', (booking_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error deleting booking: {e}")
        return False

def mark_booking_returned(booking_id, photo_id):
    """Mark booking as returned with photo."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        return_time = now_sgt().strftime("%d/%m %H:%M")
        cursor.execute(
            'UPDATE bookings SET is_returned = 1, return_photo_id = ?, return_time = ? WHERE id = ?',
            (photo_id, return_time, booking_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error marking booking returned: {e}")
        return False

def delete_expired_bookings():
    """Delete bookings where the date and time have passed."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        now = now_sgt()
        today = now.date()
        current_time = now.strftime("%H:%M")

        cursor.execute('SELECT id, booking_date, end_time FROM bookings')
        bookings = cursor.fetchall()

        for booking_id, booking_date, end_time in bookings:
            booked = parse_booking_date(booking_date, now)
            if booked is None:
                continue
            if booked < today or (booked == today and end_time <= current_time):
                cursor.execute('DELETE FROM bookings WHERE id = ?', (booking_id,))

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error deleting expired bookings: {e}")

# Conversation states
MAIN_MENU, VIEW_MY_BOOKINGS, VIEW_ALL_BOOKINGS, SELECT_DATE, SELECT_START_TIME, SELECT_END_TIME, SELECT_ICE_BOX, ENTER_NAME, CONFIRM_RETURN_PHOTO = range(9)

def generate_time_slots(booking_date=None):
    """30-minute slots from 00:00 to 23:30. When booking_date is today, slots
    that have already passed are left out, so you cannot book 10:00 at 23:00."""
    now = now_sgt()
    is_today = (booking_date is not None
                and parse_booking_date(booking_date, now) == now.date())
    current_time = now.strftime("%H:%M")

    slots = []
    for hour in range(24):
        for minute in [0, 30]:
            slot = f"{hour:02d}:{minute:02d}"
            if is_today and slot <= current_time:
                continue
            slots.append(slot)
    return slots

def generate_start_slots(booking_date=None):
    """Slots usable as a start time. The last slot of the day is excluded
    because no later slot could serve as its end time."""
    return generate_time_slots(booking_date)[:-1]

def generate_date_buttons():
    """Date buttons for the next 7 days. Today is dropped once all of its
    slots have passed, so there is no dead button to tap late at night."""
    today = now_sgt()
    dates = []
    for i in range(7):
        date = today + timedelta(days=i)
        date_str = date.strftime("%d/%m")
        if i == 0 and not generate_start_slots(date_str):
            continue
        label = f"Today ({date_str})" if i == 0 else f"Tomorrow ({date_str})" if i == 1 else date_str
        dates.append((label, date_str))
    return dates

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command."""
    try:
        delete_expired_bookings()
        keyboard = [
            [InlineKeyboardButton("📅 New Booking", callback_data="new_booking")],
            [InlineKeyboardButton("👁️ View My Bookings", callback_data="view_bookings")],
            [InlineKeyboardButton("📊 View All Bookings", callback_data="view_all_bookings")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🧊 *Ice Box Loaning Bot*\n\nWhat would you like to do?",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return MAIN_MENU
    except Exception as e:
        print(f"Error in start: {e}")
        await update.message.reply_text("❌ An error occurred. Please try /start again.")
        return MAIN_MENU

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user their Telegram ID."""
    user_id = update.message.from_user.id
    await update.message.reply_text(f"Your Telegram ID: `{user_id}`", parse_mode="Markdown")

async def admin_returns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all bookings with return status (admin view)."""
    try:
        # Check if user is admin
        user_id = update.message.from_user.id
        if ADMIN_USER_ID is None:
            await update.message.reply_text("❌ Admin user ID not configured. Contact the bot creator.")
            return

        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Access denied. Only admin can view this.")
            return

        bookings = get_all_bookings(include_returned=True)

        if not bookings:
            await update.message.reply_text("📭 No bookings yet.")
            return

        text = "👨‍💼 *ADMIN: Return Status*\n\n"
        current_box = None
        pending_count = 0
        returned_count = 0
        keyboard = []

        for booking_id, user_name, ice_box_id, start_time, end_time, booking_date in bookings:
            if current_box != ice_box_id:
                current_box = ice_box_id
                text += f"\n🧊 *Ice Box {ice_box_id}*\n"

            # Get return status
            try:
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute('SELECT is_returned, return_photo_id FROM bookings WHERE id = ?', (booking_id,))
                result = cursor.fetchone()
                conn.close()

                if result:
                    is_returned, return_photo_id = result
                    if is_returned:
                        text += f"✅ {user_name} | {booking_date} {start_time}-{end_time}\n"
                        returned_count += 1

                        # Add button to view photo if it exists
                        if return_photo_id:
                            keyboard.append([InlineKeyboardButton(
                                f"📸 View Photo - {user_name} ({ice_box_id})",
                                callback_data=f"admin_photo_{booking_id}"
                            )])
                    else:
                        text += f"⏳ {user_name} | {booking_date} {start_time}-{end_time} | PENDING\n"
                        pending_count += 1
            except Exception as e:
                print(f"Error checking return status for booking {booking_id}: {e}")

        text += f"\n\n📊 *Summary*\n✅ Returned: {returned_count}\n⏳ Pending: {pending_count}"

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        print(f"Error in admin_returns: {e}")
        await update.message.reply_text("❌ Error retrieving admin data.")

async def send_return_photo(query, context):
    """Send the stored return photo for the booking named in query.data."""
    try:
        booking_id = int(query.data.split("_")[2])

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT ice_box_id, user_name, booking_date, return_photo_id FROM bookings WHERE id = ?', (booking_id,))
        result = cursor.fetchone()
        conn.close()

        if result:
            ice_box_id, user_name, booking_date, return_photo_id = result
            if return_photo_id:
                await context.bot.send_photo(
                    chat_id=query.from_user.id,
                    photo=return_photo_id,
                    caption=f"📸 *Return Photo Verification*\n🧊 Ice Box: {ice_box_id}\n👤 Student: {user_name}\n📅 Date: {booking_date}",
                    parse_mode="Markdown"
                )
            else:
                await query.answer("No photo found for this return.", show_alert=True)
        else:
            await query.answer("Booking not found.", show_alert=True)
    except Exception as e:
        print(f"Error sending return photo: {e}")
        await query.answer("Error retrieving photo.", show_alert=True)

async def admin_photo_viewer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View a return photo (admin). Reached only when no conversation is
    active; otherwise button_callback routes to send_return_photo directly."""
    query = update.callback_query
    await query.answer()
    await send_return_photo(query, context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses."""
    try:
        delete_expired_bookings()
        query = update.callback_query
        await query.answer()

        if query.data == "new_booking":
            # Show date picker
            dates = generate_date_buttons()
            keyboard = [[InlineKeyboardButton(label, callback_data=f"date_{date_str}")]
                        for label, date_str in dates]
            keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "📅 Select a date:",
                reply_markup=reply_markup
            )
            return SELECT_DATE

        elif query.data.startswith("date_"):
            booking_date = query.data.split("_", 1)[1]
            context.user_data["booking_date"] = booking_date

            # Show start time picker (past slots omitted when booking today)
            time_slots = generate_start_slots(booking_date)
            keyboard = []
            for i in range(0, len(time_slots), 3):
                row = [InlineKeyboardButton(time_slots[j], callback_data=f"start_time_{time_slots[j]}")
                       for j in range(i, min(i + 3, len(time_slots)))]
                keyboard.append(row)

            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="new_booking")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "⏰ Select start time:",
                reply_markup=reply_markup
            )
            return SELECT_START_TIME

        elif query.data.startswith("start_time_"):
            start_time = query.data.split("_", 2)[2]
            context.user_data["start_time"] = start_time

            # Show end time picker (only times after start time)
            time_slots = generate_time_slots(context.user_data.get("booking_date"))
            start_idx = time_slots.index(start_time) if start_time in time_slots else -1
            end_slots = time_slots[start_idx + 1:] if start_idx >= 0 else []

            keyboard = []
            for i in range(0, len(end_slots), 3):
                row = [InlineKeyboardButton(end_slots[j], callback_data=f"end_time_{end_slots[j]}")
                       for j in range(i, min(i + 3, len(end_slots)))]
                keyboard.append(row)

            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="new_booking")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "⏰ Select end time:",
                reply_markup=reply_markup
            )
            return SELECT_END_TIME

        elif query.data.startswith("end_time_"):
            end_time = query.data.split("_", 2)[2]
            context.user_data["end_time"] = end_time

            # Get available ice boxes
            booking_date = context.user_data["booking_date"]
            start_time = context.user_data["start_time"]

            available = get_available_boxes(start_time, end_time, booking_date)

            if not available:
                await query.edit_message_text(
                    f"❌ No ice boxes available for {booking_date} {start_time}-{end_time}.\n\nTry a different time!"
                )
                return

            # Show available ice boxes
            keyboard = [[InlineKeyboardButton(f"🧊 Ice Box {box}", callback_data=f"ice_box_{box}")]
                        for box in available]
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="new_booking")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"✅ Available Ice Boxes for {booking_date} {start_time}-{end_time}:",
                reply_markup=reply_markup
            )
            return SELECT_ICE_BOX

        elif query.data.startswith("ice_box_"):
            ice_box_id = query.data.split("_")[2]
            context.user_data["ice_box_id"] = ice_box_id

            await query.edit_message_text("👤 Enter your name:\n\n_(Type 'cancel' to go back)_", parse_mode="Markdown")
            return ENTER_NAME

        elif query.data == "view_bookings":
            bookings = get_user_bookings(query.from_user.id)
            context.user_data["view_mode"] = "my_bookings"

            if not bookings:
                keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("📭 You have no bookings yet.", reply_markup=reply_markup)
                return VIEW_MY_BOOKINGS

            text = "📋 *Your Bookings:*\n\n"
            keyboard = []

            for booking_id, ice_box_id, start_time, end_time, booking_date, is_returned, return_photo_id in bookings:
                text += f"🧊 Ice Box {ice_box_id}\n"
                text += f"📅 {booking_date} | ⏰ {start_time} - {end_time}\n"

                if is_returned:
                    text += f"✅ *Status: Returned*\n"
                    keyboard.append([
                        InlineKeyboardButton(f"📸 View Photo", callback_data=f"view_photo_{booking_id}"),
                        InlineKeyboardButton(f"🗑️ Delete", callback_data=f"delete_{booking_id}")
                    ])
                else:
                    text += f"📅 *Status: Pending Return*\n"
                    keyboard.append([
                        InlineKeyboardButton(f"📸 Return Now", callback_data=f"confirm_return_{booking_id}"),
                        InlineKeyboardButton(f"🗑️ Delete", callback_data=f"delete_{booking_id}")
                    ])

                text += "\n"

            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            return VIEW_MY_BOOKINGS

        elif query.data == "view_all_bookings":
            bookings = get_all_bookings()
            context.user_data["view_mode"] = "all_bookings"

            if not bookings:
                keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("📭 No bookings yet.", reply_markup=reply_markup)
                return VIEW_ALL_BOOKINGS

            text = "📊 *All Bookings:*\n\n"
            current_box = None
            for booking_id, user_name, ice_box_id, start_time, end_time, booking_date in bookings:
                if current_box != ice_box_id:
                    current_box = ice_box_id
                    text += f"\n🧊 *Ice Box {ice_box_id}*\n"
                text += f"👤 {user_name} | 📅 {booking_date} | ⏰ {start_time}-{end_time}\n"

            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            return VIEW_ALL_BOOKINGS

        elif query.data.startswith("delete_"):
            booking_id = int(query.data.split("_")[1])
            delete_booking(booking_id)
            keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("✅ Booking deleted!", reply_markup=reply_markup)

            # Return to the appropriate view state
            view_mode = context.user_data.get("view_mode")
            if view_mode == "my_bookings":
                return VIEW_MY_BOOKINGS
            elif view_mode == "all_bookings":
                return VIEW_ALL_BOOKINGS
            return MAIN_MENU

        elif query.data.startswith("confirm_return_"):
            booking_id = int(query.data.split("_")[2])
            context.user_data["return_booking_id"] = booking_id
            await query.edit_message_text(
                "📸 Please send a photo of the returned ice box to confirm:\n\n_(Type 'cancel' to go back)_",
                parse_mode="Markdown"
            )
            return CONFIRM_RETURN_PHOTO

        elif query.data.startswith("admin_photo_"):
            # The conversation's catch-all CallbackQueryHandler is registered
            # ahead of the dedicated admin handler and would otherwise swallow
            # this callback, so the photo is sent from here.
            await send_return_photo(query, context)
            return MAIN_MENU

        elif query.data.startswith("view_photo_"):
            booking_id = int(query.data.split("_")[2])
            bookings = get_user_bookings(query.from_user.id, include_returned=True)
            booking = [b for b in bookings if b[0] == booking_id]

            if booking:
                return_photo_id = booking[0][6]
                if return_photo_id:
                    await query.answer()
                    await context.bot.send_photo(
                        chat_id=query.from_user.id,
                        photo=return_photo_id,
                        caption=f"🧊 Return Photo - Ice Box {booking[0][1]}\n📅 {booking[0][4]}"
                    )
                else:
                    await query.answer("❌ No photo found for this return.", show_alert=True)
            else:
                await query.answer("❌ Booking not found.", show_alert=True)

        elif query.data == "back_to_menu":
            keyboard = [
                [InlineKeyboardButton("📅 New Booking", callback_data="new_booking")],
                [InlineKeyboardButton("👁️ View My Bookings", callback_data="view_bookings")],
                [InlineKeyboardButton("📊 View All Bookings", callback_data="view_all_bookings")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("🧊 Main Menu", reply_markup=reply_markup)
            return MAIN_MENU

        return MAIN_MENU
    except Exception as e:
        print(f"Error in button_callback: {e}")
        try:
            await query.answer("❌ An error occurred.", show_alert=True)
        except:
            pass
        return MAIN_MENU

async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for booking name and photo returns."""
    try:
        user_id = update.message.from_user.id

        if "return_booking_id" in context.user_data:
            # Handle photo for return
            text = update.message.text.strip().lower() if update.message.text else ""

            # Check for cancel command
            if text in ["cancel", "back"]:
                keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("Cancelled. Returning to menu...", reply_markup=reply_markup)
                context.user_data.clear()
                return MAIN_MENU

            if update.message.photo:
                photo = update.message.photo[-1]
                booking_id = context.user_data["return_booking_id"]
                if mark_booking_returned(booking_id, photo.file_id):
                    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text("✅ Return confirmed! 📸 Photo saved.", reply_markup=reply_markup, parse_mode="Markdown")
                else:
                    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text("❌ Error saving return. Please try again.", reply_markup=reply_markup, parse_mode="Markdown")
                context.user_data.clear()
                # Stay in MAIN_MENU: ending here would leave the Back to Menu
                # button above with no handler, forcing the user to type /start.
                return MAIN_MENU
            else:
                await update.message.reply_text("❌ Please send a photo!\n\n_(Type 'cancel' to go back)_", parse_mode="Markdown")
                return CONFIRM_RETURN_PHOTO

        elif "ice_box_id" in context.user_data:
            # Handle name entry
            text = update.message.text.strip()

            # Check for cancel command
            if text.lower() in ["cancel", "back"]:
                keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("Booking cancelled. Returning to menu...", reply_markup=reply_markup)
                context.user_data.clear()
                return MAIN_MENU

            user_name = text
            ice_box_id = context.user_data.get("ice_box_id")
            start_time = context.user_data.get("start_time")
            end_time = context.user_data.get("end_time")
            booking_date = context.user_data.get("booking_date")

            booking_id = add_booking(user_id, user_name, ice_box_id, start_time, end_time, booking_date)

            if booking_id:
                keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    f"✅ *Booking Confirmed!*\n\n"
                    f"🧊 Ice Box {ice_box_id}\n"
                    f"👤 Name: {user_name}\n"
                    f"📅 Date: {booking_date}\n"
                    f"⏰ Time: {start_time} - {end_time}",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            else:
                keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "❌ Error creating booking. This time slot may no longer be available.",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )

            context.user_data.clear()
            # Stay in MAIN_MENU: ending here would leave the Back to Menu
            # button above with no handler, forcing the user to type /start.
            return MAIN_MENU

    except Exception as e:
        print(f"Error in receive_text: {e}")
        import traceback
        traceback.print_exc()
        await update.message.reply_text("❌ An error occurred. Please try again.\n\nType /start to return to menu.")
        context.user_data.clear()
        return ConversationHandler.END

def run_bot():
    """Run the bot."""
    try:
        init_db()

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set")

        app = Application.builder().token(token).build()

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                MAIN_MENU: [CallbackQueryHandler(button_callback)],
                VIEW_MY_BOOKINGS: [CallbackQueryHandler(button_callback)],
                VIEW_ALL_BOOKINGS: [CallbackQueryHandler(button_callback)],
                SELECT_DATE: [CallbackQueryHandler(button_callback)],
                SELECT_START_TIME: [CallbackQueryHandler(button_callback)],
                SELECT_END_TIME: [CallbackQueryHandler(button_callback)],
                SELECT_ICE_BOX: [CallbackQueryHandler(button_callback)],
                ENTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
                CONFIRM_RETURN_PHOTO: [
                    MessageHandler(filters.PHOTO, receive_text),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)
                ],
            },
            fallbacks=[CommandHandler("start", start)],
        )

        app.add_handler(conv_handler)
        app.add_handler(CommandHandler("myid", myid))
        app.add_handler(CommandHandler("admin_returns", admin_returns))
        app.add_handler(CallbackQueryHandler(admin_photo_viewer, pattern="^admin_photo_"))

        print("🚀 Bot is running...")
        app.run_polling()
    except Exception as e:
        print(f"Error in run_bot: {e}")
        raise

def main():
    """Start the bot."""
    try:
        run_bot()
    except Exception as e:
        print(f"Error in main: {e}")

if __name__ == "__main__":
    main()
