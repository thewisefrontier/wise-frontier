import sqlite3

conn = sqlite3.connect("frontier.db")

cursor = conn.cursor()

cursor.execute("""
SELECT source, title, link
FROM articles
ORDER BY id DESC
LIMIT 10
""")

rows = cursor.fetchall()

for row in rows:
    print("SOURCE:", row[0])
    print("TITLE :", row[1])
    print("LINK  :", row[2])
    print("-" * 50)

conn.close()