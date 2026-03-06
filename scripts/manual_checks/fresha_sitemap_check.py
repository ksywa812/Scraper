import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scraper import scrape_fresha

print("Running Fresha sitemap test...")
res = scrape_fresha("spa", "Wrocław", max_pages=1, use_headless=False)
print("Got", len(res), "results")
