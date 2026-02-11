import pandas as pd
from pathlib import Path
base = Path(__file__).resolve().parent
file = base / 'scraped' / 'compare_test_vs_new_08.csv'
df = pd.read_csv(file)
print('Comparison rows:', len(df))
print(df.head(10).to_string(index=False))
