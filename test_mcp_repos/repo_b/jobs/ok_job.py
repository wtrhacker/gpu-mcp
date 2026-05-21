from pathlib import Path

root = Path(__file__).resolve().parents[1]
(root / 'results' / 'marker.txt').write_text('repo-b-ran')
print('repo-b-ran')
