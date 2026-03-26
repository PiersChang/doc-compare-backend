import sqlite3

conn = sqlite3.connect('doc_compare.db')
migrations = [
    "ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN referral_code TEXT",
    "ALTER TABLE users ADD COLUMN referred_by INTEGER",
    "ALTER TABLE usage_log ADD COLUMN source TEXT NOT NULL DEFAULT 'free'",
]
for sql in migrations:
    try:
        conn.execute(sql)
        print("OK:", sql[:50])
    except Exception as e:
        print("Skip:", e)

conn.commit()
conn.close()
print("完成！")
