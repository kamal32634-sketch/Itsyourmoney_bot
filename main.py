import os
import sqlite3
import time
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from tonutils.client import ToncenterClient
from tonutils.wallet import WalletV4R2

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 8425471181  # <--- APNI ID DALEIN
LOG_GROUP_ID = -1003873011275 # <--- GROUP ID DALEIN
MINING_RATE = 0.00001 
MAX_LIMIT = 4.99999

# --- DB PATH FIX FOR GITHUB ACTIONS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot_data.db")

SET_API, SET_WORDS, GET_ADDR, BROADCAST = range(4)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (user_id INTEGER PRIMARY KEY, balance REAL, last_time REAL, 
                       username TEXT, total_withdrawn REAL DEFAULT 0.0, is_blocked INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def db_op(q, p=(), fetch=False, commit=False):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    cursor = conn.cursor()
    cursor.execute(q, p)
    res = cursor.fetchall() if fetch else None
    if commit: conn.commit()
    conn.close()
    return res

def get_bal(uid, uname="Unknown"):
    now = time.time()
    row = db_op("SELECT balance, last_time, is_blocked FROM users WHERE user_id=?", (uid,), fetch=True)
    if row:
        if row[0][2] == 1: return "BLOCKED"
        bal, lt = row[0][0], row[0][1]
        new_bal = bal + ((now - lt) * MINING_RATE)
        db_op("UPDATE users SET balance=?, last_time=?, username=? WHERE user_id=?", (new_bal, now, uname, uid), commit=True)
        return new_bal
    db_op("INSERT INTO users (user_id, balance, last_time, username) VALUES (?, 0.0, ?, ?)", (uid, now, uname), commit=True)
    return 0.0

def get_meter(bal):
    colors = ["🔴","🟠","🟡","🟢","🔵","🟣","🟤","⚪","⚫","🟩","🟦","🟥"]
    progress = int((min(bal, MAX_LIMIT) / MAX_LIMIT) * 11)
    return "".join(colors[:progress+1])

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid, uname = u.effective_user.id, u.effective_user.username or "User"
    bal = get_bal(uid, uname)
    if bal == "BLOCKED": return await u.message.reply_text("Error 404: Account Not Found.")
    
    cur_time = datetime.datetime.now().strftime("%H:%M:%S")
    kb = [[InlineKeyboardButton("🔄 Refresh", callback_data='ref')],
          [InlineKeyboardButton("💸 Withdraw", callback_data='wd')]]
    if uid == OWNER_ID: kb.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data='adm')])

    await u.message.reply_text(
        f"🖥 **Dashboard**\n\n👤 User: @{uname}\n💰 Balance: `{bal:.8f}` TON\n"
        f"📊 Progress: {get_meter(bal)}\n🕒 Time: {cur_time}\n🚀 Limit: {MAX_LIMIT} TON",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'
    )

async def admin_all(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id != OWNER_ID: return
    users = db_op("SELECT user_id, username, total_withdrawn FROM users", fetch=True)
    kb = [[InlineKeyboardButton(f"@{u[1]} (W: {u[2]:.2f})", callback_data=f"manage_{u[0]}")] for u in users]
    await u.message.reply_text("👤 **Owner Controls:**", reply_markup=InlineKeyboardMarkup(kb))

async def cb_handler(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    uid, data = q.from_user.id, q.data
    await q.answer()
    bal = get_bal(uid)
    if bal == "BLOCKED" and not data.startswith(("manage_", "block_", "unblock_")): return

    if data == 'ref':
        cur_time = datetime.datetime.now().strftime("%H:%M:%S")
        await q.edit_message_text(
            f"🖥 **Dashboard**\n\n💰 Balance: `{bal:.8f}` TON\n📊 {get_meter(bal)}\n🕒 {cur_time}",
            reply_markup=q.message.reply_markup, parse_mode='Markdown'
        )
    elif data == 'wd':
        if bal < 0.1: return await q.message.reply_text("Min 0.1 TON required.")
        await q.message.reply_text("Send TON Wallet Address:"); return GET_ADDR
    elif data.startswith("manage_"):
        tid = data.split("_")[1]
        kb = [[InlineKeyboardButton("🚫 Block", callback_data=f"block_{tid}"), InlineKeyboardButton("✅ Unblock", callback_data=f"unblock_{tid}")]]
        await q.message.reply_text(f"Admin Action UID: {tid}", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("block_"):
        db_op("UPDATE users SET is_blocked=1 WHERE user_id=?", (data.split("_")[1],), commit=True)
        await q.edit_message_text("✅ Error 404: Blocked.")
    elif data.startswith("unblock_"):
        db_op("UPDATE users SET is_blocked=0 WHERE user_id=?", (data.split("_")[1],), commit=True)
        await q.edit_message_text("✅ User Unblocked.")
    elif data == 'adm' and uid == OWNER_ID:
        kb = [[InlineKeyboardButton("Set API", callback_data='s_api')], [InlineKeyboardButton("Set Words", callback_data='s_wor')]]
        await q.edit_message_text("⚙️ Admin Settings:", reply_markup=InlineKeyboardMarkup(kb))
    elif data == 's_api': await q.message.reply_text("Send API Key:"); return SET_API
    elif data == 's_wor': await q.message.reply_text("Send 24 Words:"); return SET_WORDS

async def withdraw_f(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid, addr = u.effective_user.id, u.message.text
    bal = get_bal(uid)
    amt = min(bal, MAX_LIMIT)
    api_res = db_op("SELECT value FROM settings WHERE key='api'", fetch=True)
    wor_res = db_op("SELECT value FROM settings WHERE key='wor'", fetch=True)
    
    if not api_res or not wor_res: return await u.message.reply_text("❌ Admin setup missing.")
    
    status = await u.message.reply_text("⏳ Processing Blockchain Transaction...")
    try:
        client = ToncenterClient(api_key=api_res[0][0])
        wallet = WalletV4R2.from_mnemonic(client, wor_res[0][0].split())
        seqno = await wallet.get_seqno()
        await wallet.transfer(destination=addr, amount=amt-0.02, body="Mining Payout", seqno=seqno)
        
        db_op("UPDATE users SET balance=0, last_time=?, total_withdrawn=total_withdrawn+? WHERE user_id=?", (time.time(), amt, uid), commit=True)
        
        msg = f"✅ Withdraw Successful!\n👤 @{u.effective_user.username}\n💰 Amount: {amt:.4f} TON\n🏦 Wallet: `{addr}`"
        await status.edit_text(msg, parse_mode='Markdown')
        await c.bot.send_message(LOG_GROUP_ID, msg, parse_mode='Markdown')
    except:
        err = f"❌ Withdraw Failed!\n👤 @{u.effective_user.username}\n⚠️ Error: No Fees/Setup Issue."
        await status.edit_text(err)
        await c.bot.send_message(LOG_GROUP_ID, err)
    return ConversationHandler.END

async def broadcast_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id == OWNER_ID:
        await u.message.reply_text("Send broadcast note:"); return BROADCAST

async def broadcast_send(u: Update, c: ContextTypes.DEFAULT_TYPE):
    users = db_op("SELECT user_id FROM users", fetch=True)
    for user in users:
        try: await c.bot.send_message(user[0], f"📢 **Note:**\n\n{u.message.text}", parse_mode='Markdown')
        except: continue
    await u.message.reply_text("✅ Sent."); return ConversationHandler.END

async def s_api(u: Update, c: ContextTypes.DEFAULT_TYPE):
    db_op("INSERT OR REPLACE INTO settings VALUES ('api', ?)", (u.message.text,), commit=True)
    await u.message.reply_text("✅ API Saved."); return ConversationHandler.END

async def s_wor(u: Update, c: ContextTypes.DEFAULT_TYPE):
    db_op("INSERT OR REPLACE INTO settings VALUES ('wor', ?)", (u.message.text,), commit=True)
    await u.message.reply_text("✅ 24 Words Saved."); return ConversationHandler.END

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_handler), CommandHandler('Msg', broadcast_start)],
        states={SET_API: [MessageHandler(filters.TEXT, s_api)], SET_WORDS: [MessageHandler(filters.TEXT, s_wor)], 
                GET_ADDR: [MessageHandler(filters.TEXT, withdraw_f)], BROADCAST: [MessageHandler(filters.TEXT, broadcast_send)]},
        fallbacks=[CommandHandler('start', start)]
    )
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('all', admin_all))
    app.add_handler(conv)
    app.run_polling()
