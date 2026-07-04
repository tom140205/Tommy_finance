import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
from supabase import create_client, Client

# Load env variables
load_dotenv()

# Initialize Supabase
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

# Enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# --- CONVERSATION STATES ---
# Add Expense States
ADD_AMOUNT, ADD_DESC, ADD_SPLIT_TYPE, ADD_SELECT_MEMBERS = range(4)
# Settle States
SETTLE_USER, SETTLE_PHOTO = range(4, 6)

# --- CORE FUNCTIONS ---

async def register_user_and_group(user, chat):
    """Silently registers the user and group to the DB if they don't exist."""
    try:
        supabase.table("users").upsert({
            "telegram_id": user.id,
            "username": user.username,
            "phone_number": None # Handled separately if needed
        }).execute()

        if chat.type in ['group', 'supergroup']:
            supabase.table("groups").upsert({
                "chat_id": chat.id,
                "group_name": chat.title
            }).execute()
            
            supabase.table("group_members").upsert({
                "group_id": chat.id,
                "user_id": user.id
            }).execute()
    except Exception as e:
        logging.error(f"DB Error during registration: {e}")

async def babu_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered by /babu or the word 'babu'"""
    user = update.effective_user
    chat = update.effective_chat
    
    await register_user_and_group(user, chat)

    keyboard = [
        [InlineKeyboardButton("➕ Add Expense", callback_data='menu_add')],
        [InlineKeyboardButton("⚖️ Check Balance", callback_data='menu_balance')],
        [InlineKeyboardButton("💸 Settle Up", callback_data='menu_settle')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Use reply_text if it's a message, edit_message_text if it's a callback
    text = f"Yo {user.first_name}, Babu is here! What do we need to do?"
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

# --- MENU ROUTER ---
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes the initial menu clicks to their respective flows"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'menu_balance':
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Query the Postgres View we created
        res = supabase.table('group_user_balances').select('*').eq('group_id', chat_id).eq('user_id', user_id).execute()
        
        if not res.data:
            await query.edit_message_text("No balances found for you in this group yet.")
            return

        balance = res.data[0]['net_balance']
        if balance > 0:
            text = f"You are owed a total of ₹{balance} in this group. 🤑"
        elif balance < 0:
            text = f"You owe a total of ₹{abs(balance)} in this group. 📉"
        else:
            text = "You are completely settled up! 🍻"
            
        await query.edit_message_text(text)

# --- ADD EXPENSE FLOW ---

async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("💸 How much did you spend? (Type the amount)")
    return ADD_AMOUNT

async def add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        context.user_data['amount'] = amount
        await update.message.reply_text(f"Got it: ₹{amount}. What was this for?")
        return ADD_DESC
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return ADD_AMOUNT

async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['desc'] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton("🔀 Split Equally with All", callback_data='split_all')],
        [InlineKeyboardButton("👥 Select Persons", callback_data='split_select')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
    ]
    await update.message.reply_text("How do you want to split this?", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_SPLIT_TYPE

async def handle_split_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    
    if query.data == 'cancel':
        await query.edit_message_text("Expense cancelled.")
        context.user_data.clear()
        return ConversationHandler.END
        
    if query.data == 'split_all':
        # Fetch all members
        res = supabase.table("group_members").select("user_id").eq("group_id", chat_id).execute()
        members = [m['user_id'] for m in res.data]
        await finalize_expense(update, context, members)
        return ConversationHandler.END
        
    if query.data == 'split_select':
        # Fetch all members with their usernames to build the selection keyboard
        res = supabase.table("group_members").select("user_id, users(username)").eq("group_id", chat_id).execute()
        
        # Initialize selected set in context
        context.user_data['selected_users'] = set()
        context.user_data['group_users'] = res.data
        
        await render_selection_keyboard(query, context)
        return ADD_SELECT_MEMBERS

async def render_selection_keyboard(query, context):
    """Builds a togglable inline keyboard for user selection"""
    keyboard = []
    selected = context.user_data['selected_users']
    
    for member in context.user_data['group_users']:
        uid = member['user_id']
        username = member['users']['username'] or f"User {uid}"
        # Toggle checkmark
        mark = "✅ " if uid in selected else ""
        keyboard.append([InlineKeyboardButton(f"{mark}{username}", callback_data=f"toggle_{uid}")])
        
    keyboard.append([InlineKeyboardButton("➡️ Confirm Split", callback_data="confirm_split")])
    await query.edit_message_text("Select who is involved:", reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_split":
        selected = list(context.user_data['selected_users'])
        if not selected:
            await query.edit_message_text("You must select at least one person! Process cancelled.")
            return ConversationHandler.END
        
        await finalize_expense(update, context, selected)
        return ConversationHandler.END
        
    # Handle toggle
    uid = int(query.data.split("_")[1])
    if uid in context.user_data['selected_users']:
        context.user_data['selected_users'].remove(uid)
    else:
        context.user_data['selected_users'].add(uid)
        
    await render_selection_keyboard(query, context)
    return ADD_SELECT_MEMBERS

async def finalize_expense(update, context, target_user_ids):
    chat_id = update.effective_chat.id
    paid_by = update.effective_user.id
    amount = context.user_data['amount']
    desc = context.user_data['desc']
    
    # 1. Insert main expense
    exp_res = supabase.table("expenses").insert({
        "group_id": chat_id, "paid_by": paid_by, "amount": amount, "description": desc
    }).execute()
    expense_id = exp_res.data[0]['id']
    
    # 2. Insert splits
    split_amt = round(amount / len(target_user_ids), 2)
    splits = [{"expense_id": expense_id, "user_id": uid, "amount_owed": split_amt} for uid in target_user_ids]
    supabase.table("expense_splits").insert(splits).execute()
    
    await update.callback_query.edit_message_text(
        f"✅ **Expense Added!**\n₹{amount} for {desc}\nSplit among {len(target_user_ids)} people (₹{split_amt} each)."
    )
    context.user_data.clear()

# --- SETTLE UP FLOW ---

async def start_settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    
    # Get group members to pay
    res = supabase.table("group_members").select("user_id, users(username)").eq("group_id", chat_id).execute()
    
    keyboard = []
    for member in res.data:
        uid = member['user_id']
        username = member['users']['username'] or f"User {uid}"
        # Prevent settling with yourself
        if uid != update.effective_user.id:
            keyboard.append([InlineKeyboardButton(username, callback_data=f"pay_{uid}")])
            
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await query.edit_message_text("Who are you paying?", reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTLE_USER

async def select_settle_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'cancel':
        await query.edit_message_text("Settlement cancelled.")
        return ConversationHandler.END
        
    payee_id = int(query.data.split("_")[1])
    context.user_data['payee_id'] = payee_id
    
    await query.edit_message_text(
        "Great! Send a photo of the payment screenshot in this chat. \n\n**Important:** Put the amount you paid as the photo's caption (e.g., 500)"
    )
    return SETTLE_PHOTO

async def process_settlement_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    payer_id = update.effective_user.id
    payee_id = context.user_data['payee_id']
    message_id = update.message.message_id
    caption = update.message.caption
    
    try:
        amount = float(caption)
    except (ValueError, TypeError):
        await update.message.reply_text("I couldn't find a valid amount in the caption. Please send the photo again with just the number as the caption.")
        return SETTLE_PHOTO
        
    # Insert settlement record with the telegram message ID as proof
    supabase.table("settlements").insert({
        "group_id": chat_id,
        "from_user": payer_id,
        "to_user": payee_id,
        "amount": amount,
        "message_id": message_id,
        "status": "settled"
    }).execute()
    
    # We also need to log this as an "expense" where payer paid payee directly to balance the books
    # A settlement is effectively an expense where the split is 100% on the payee
    exp_res = supabase.table("expenses").insert({
        "group_id": chat_id, "paid_by": payer_id, "amount": amount, "description": "Settlement Payment"
    }).execute()
    
    supabase.table("expense_splits").insert({
        "expense_id": exp_res.data[0]['id'], "user_id": payee_id, "amount_owed": amount
    }).execute()

    await update.message.reply_text(
        f"✅ Settlement recorded! ₹{amount} paid. Proof anchored to message ID #{message_id}.",
        reply_to_message_id=message_id
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# --- MAIN ENGINE ---

def main():
    app = Application.builder().token(os.environ.get("TELEGRAM_BOT_TOKEN")).build()

    # Trigger Menu (by /babu command or typing 'babu')
    app.add_handler(CommandHandler("babu", babu_trigger))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)\bbabu\b'), babu_trigger))

    # Catch simple menu clicks (like Check Balance)
    app.add_handler(CallbackQueryHandler(menu_router, pattern='^menu_balance$'))

    # Add Expense Flow
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add, pattern='^menu_add$')],
        states={
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
            ADD_SPLIT_TYPE: [CallbackQueryHandler(handle_split_type)],
            ADD_SELECT_MEMBERS: [CallbackQueryHandler(toggle_members)]
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
        per_message=False
    )

    # Settle Up Flow
    settle_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_settle, pattern='^menu_settle$')],
        states={
            SETTLE_USER: [CallbackQueryHandler(select_settle_user)],
            SETTLE_PHOTO: [MessageHandler(filters.PHOTO, process_settlement_photo)]
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
        per_message=False
    )

    app.add_handler(add_conv)
    app.add_handler(settle_conv)

    print("Babu is awake and listening...")
    app.run_polling()

if __name__ == '__main__':
    main()
