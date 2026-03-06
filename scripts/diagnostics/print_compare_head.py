from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
file = PROJECT_ROOT / "Data" / "Raport" / "compare_test_vs_new_08.csv"
df = pd.read_csv(file)
print("Comparison rows:", len(df))
print(df.head(10).to_string(index=False))
