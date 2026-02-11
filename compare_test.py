import pandas as pd
from pathlib import Path

base = Path(__file__).resolve().parent
file_a = base / 'scraped' / 'test_spa_wroclaw.xlsx'
file_b = base / 'scraped' / 'new_08.xlsx'

print('Files exist:', file_a.exists(), file_b.exists())
A = pd.read_excel(file_a)
B = pd.read_excel(file_b)

A = A.fillna('')
B = B.fillna('')
A['key'] = A['Name'].str.lower().str.replace(r"[^\w\s]", "", regex=True).str.strip() + '|' + A['Address'].str.lower().str.strip()
B['key'] = B['Name'].str.lower().str.replace(r"[^\w\s]", "", regex=True).str.strip() + '|' + B['Address'].str.lower().str.strip()

A['emails_all'] = (A[['Email 1','Email 2','Email 3','Other Emails']].astype(str).agg(','.join, axis=1).str.replace(r',+', ',', regex=True).str.strip(','))
B['emails_all'] = (B[['Email 1','Email 2','Email 3','Other Emails']].astype(str).agg(','.join, axis=1).str.replace(r',+', ',', regex=True).str.strip(','))
A['emails_set'] = A['emails_all'].apply(lambda s: set([e.strip() for e in s.split(',') if e.strip()]))
B['emails_set'] = B['emails_all'].apply(lambda s: set([e.strip() for e in s.split(',') if e.strip()]))

keys_a = set(A['key'])
keys_b = set(B['key'])
only_a = keys_a - keys_b
only_b = keys_b - keys_a
both = keys_a & keys_b

print('Counts: A=', len(A), 'B=', len(B))
print('Unique keys: A=', len(keys_a), 'B=', len(keys_b), 'Both=', len(both))
print('Only in A:', len(only_a), 'Only in B:', len(only_b))

print('\nExamples only in A (first 5):')
for k in list(only_a)[:5]:
    r = A[A['key']==k].iloc[0]
    print('-', r['Name'], '|', r['Address'], '| Emails:', r['emails_all'], '| Sources:', r.get('Sources',''))

print('\nExamples only in B (first 5):')
for k in list(only_b)[:5]:
    r = B[B['key']==k].iloc[0]
    print('-', r['Name'], '|', r['Address'], '| Emails:', r['emails_all'], '| Sources:', r.get('Sources',''))

email_diffs = []
for k in list(both):
    a = A[A['key']==k].iloc[0]
    b = B[B['key']==k].iloc[0]
    if a['emails_set'] != b['emails_set']:
        email_diffs.append((k, a['emails_set'], b['emails_set'], a.get('Sources',''), b.get('Sources','')))

print('\nCommon keys with different emails:', len(email_diffs))
print('\nExamples of differing emails in common records (first 5):')
for k,a_em,b_em,asrc,bsrc in email_diffs[:5]:
    print('-', k)
    print('   Test:', a_em, 'sources:', asrc)
    print('   new_08:', b_em, 'sources:', bsrc)

out = base / 'scraped' / 'compare_test_vs_new_08.csv'
rows = []
for k in sorted(list(both)):
    a = A[A['key']==k].iloc[0]
    b = B[B['key']==k].iloc[0]
    rows.append({'key':k,'name':a['Name'],'address':a['Address'],'emails_test':','.join(sorted(a['emails_set'])),'emails_new08':','.join(sorted(b['emails_set'])),'sources_test':a.get('Sources',''),'sources_new08':b.get('Sources','')})

pd.DataFrame(rows).to_csv(out,index=False)
print('\nSaved comparison to', out)
