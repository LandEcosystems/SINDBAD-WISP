"""Verify committed compact analysis artifacts before reuse or downstream work."""
import json
from pathlib import Path
from .io import file_hash


def verify_region_artifacts(directory):
    directory = Path(directory)
    marker = directory / 'completion.json'
    receipt = json.loads(marker.read_text())
    artifacts = receipt.get('artifacts', [])
    checksums = receipt.get('checksums', {})
    if not artifacts or set(artifacts) != set(checksums):
        raise RuntimeError('Incomplete artifact checksum receipt: ' + str(marker))
    for name in artifacts:
        path = directory / name
        if Path(name).is_absolute() or path.resolve().parent != directory.resolve():
            raise RuntimeError('Artifact is not a direct region output: ' + name)
        if not path.is_file() or file_hash(path) != checksums[name]:
            raise RuntimeError('Missing or modified analysis artifact: ' + str(path))
    return {'region_id': directory.name, 'verified_artifacts': len(artifacts)}
