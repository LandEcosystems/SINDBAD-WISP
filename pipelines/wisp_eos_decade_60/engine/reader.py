"""Read seven already-normalized tensors from the complete decade input index."""
import bisect
import json
from pathlib import Path


class DecadeInput:
    def __init__(self, manifest):
        self.manifest = json.loads(Path(manifest).read_text())
        if not self.manifest.get('passed') or self.manifest.get('days') != 3641 or len(self.manifest.get('months', [])) != 120:
            raise ValueError('A fully verified decade manifest is required')
        self.months = self.manifest['months']
        self.ends = []
        n = 0
        for month in self.months:
            n += month['days']
            self.ends.append(n)
        if n != 3641:
            raise ValueError('Incomplete decade index')

    def tensors(self, date):
        import datetime
        from wisp_batch.store import InputStore
        day = datetime.date.fromisoformat(str(date)[:10])
        index = (day - datetime.date(2014, 1, 12)).days
        if not 0 <= index < 3641:
            raise IndexError('Date outside 2014-01-12 through 2023-12-31')
        month_index = bisect.bisect_right(self.ends, index)
        month = self.months[month_index]
        path = Path(month['path'])
        if not path.exists():
            raise FileNotFoundError('Temporary inputs have been cleaned after verified prediction: ' + str(path))
        store = InputStore(path)
        if store.fingerprint != month['input_fingerprint']:
            raise ValueError('Input month identity changed')
        start = 0 if month_index == 0 else self.ends[month_index - 1]
        return store.tensors(index - start)  # No further normalization.
