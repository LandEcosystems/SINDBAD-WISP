"""Atomic outputs, immutable snapshots and OS-released exclusive locks."""
import contextlib, fcntl, hashlib, json, os, tempfile
from pathlib import Path
import numpy as np

def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()

def file_hash(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp.'+str(os.getpid()))
    def convert(x):
        if isinstance(x,np.generic): return x.item()
        if isinstance(x,np.ndarray): return x.tolist()
        return str(x)
    with tmp.open('w') as f:
        json.dump(value,f,indent=2,default=convert);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

@contextlib.contextmanager
def exclusive(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

def source_identity(paths):
    return [dict(path=str(p),size=Path(p).stat().st_size,mtime_ns=Path(p).stat().st_mtime_ns) for p in paths]
