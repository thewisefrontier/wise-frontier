import requests

BOT_TOKEN = "8683304529:AAEU5OHkwZUUJgZS-Vd-mJZfepSvNHeZ8dA"
CHAT_ID = "@TheWiseFrontier"

message = """
Wise Frontier Test

System online.
"""

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

payload = {
    "chat_id": CHAT_ID,
    "text": message
}

response = requests.post(url, data=payload)

print(response.text)