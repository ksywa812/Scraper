import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scraper import save_to_excel

# Small synthetic test item for insertion under Bydgoszcz
item = {
    "name": "TEST APPEND CITY SPA",
    "formatted_address": "ul. Testowa 1, 85-001 Bydgoszcz",
    "formatted_phone_number": "500 500 500",
    "emails": ["append-test@example.com"],
    "sources": ["testsource"],
}

save_to_excel(
    [item],
    filename=os.path.join("Data", "Raport", "IdeaMusicLeads.xlsx"),
    city="Bydgoszcz",
    append=True,
)
print("Test append executed")
