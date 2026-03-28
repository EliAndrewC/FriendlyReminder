"""One-time script to set up the spreadsheet with labels and explanations."""
import json
import gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_file(
    "woodlawn-sms-d74fa6940b5b.json",
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
gc = gspread.authorize(creds)
sheet = gc.open_by_key("1LFCGIA-77Vl_Lg8HsLcq3i5o5u52T8L17KUs9Br66a0")

tabs = {
    "Admins": "Phone numbers listed here can sign in to the Meeting SMS app and send messages.",
    "Test": "Messages sent in test mode go only to these numbers. Use this to verify the system works before sending to everyone.",
    "Recipients": "Everyone who should receive Meeting SMS notifications. Messages sent in real mode go to all of these numbers.",
}

for tab_name, description in tabs.items():
    ws = sheet.worksheet(tab_name)
    ws.clear()
    ws.update("A1:B1", [[tab_name, description]])
    ws.update("A2:B2", [["Name", "Phone"]])
    ws.format("A1", {"textFormat": {"bold": True, "fontSize": 12}})
    ws.format("B1", {"textFormat": {"italic": True}})
    ws.format("A2:B2", {"textFormat": {"bold": True}})

print("Done — spreadsheet is set up.")
