import sqlite3

conn = sqlite3.connect("frontier.db")

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    title TEXT,
    link TEXT UNIQUE
)
""")

conn.commit()

print("Database initialized.")

conn.close()