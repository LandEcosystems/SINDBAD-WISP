"""Small independently testable numerical contracts."""
import numpy as np
import pandas as pd

def rounding_bound(x, bits=14):
    """Conservative half-ULP bound for Julia roundToDigits(bits=ceil(4log2(10)))."""
    a = np.abs(np.asarray(x, dtype=np.float64))
    _, exp = np.frexp(a)
    return np.where(a == 0, 0., np.ldexp(np.ones_like(a), exp-bits-2))

def metrics(reference, other, weights=None):
    r, o = np.asarray(reference, float).ravel(), np.asarray(other, float).ravel()
    w = np.ones_like(r) if weights is None else np.asarray(weights, float).ravel()
    ok = np.isfinite(r) & np.isfinite(o) & np.isfinite(w) & (w > 0)
    r,o,w = r[ok],o[ok],w[ok]
    if not len(r): return dict(n=0,bias=np.nan,mae=np.nan,rmse=np.nan,r=np.nan,r2=np.nan,nse=np.nan)
    w /= w.sum(); d=o-r; rc=r-np.sum(w*r); oc=o-np.sum(w*o)
    vr=np.sum(w*rc*rc); vo=np.sum(w*oc*oc)
    corr=np.sum(w*rc*oc)/np.sqrt(vr*vo) if vr>0 and vo>0 else np.nan
    return dict(n=len(r),bias=float(w@d),mae=float(w@np.abs(d)),rmse=float(np.sqrt(w@(d*d))),r=float(corr),r2=float(corr*corr),nse=float(1-w@(d*d)/vr) if vr>0 else np.nan)

def time_groups(dates):
    dates=pd.DatetimeIndex(dates)
    months=dates.to_period('M').unique()
    years=np.unique(dates.year)
    composites=[]
    for y in years:
        jan=pd.Timestamp(int(y),1,1); end=pd.Timestamp(int(y)+1,1,1)
        for d in range(0,(end-jan).days,8):
            a=jan+pd.Timedelta(days=d); b=min(a+pd.Timedelta(days=8),end)
            composites.append((a,b))
    return dict(
        month=[(str(p),np.flatnonzero(dates.to_period('M')==p),p.start_time,(p+1).start_time) for p in months],
        year=[(str(y),np.flatnonzero(dates.year==y),pd.Timestamp(int(y),1,1),pd.Timestamp(int(y)+1,1,1)) for y in years],
        composite=[(str(a.date()),np.flatnonzero((dates>=a)&(dates<b)),a,b) for a,b in composites])

def period_reduce(x, indices, kind):
    """No skipna: any missing day propagates at that pixel."""
    a=np.asarray(x)[...,indices]
    if not len(indices): return np.full(a.shape[:-1],np.nan)
    return np.sum(a,axis=-1,dtype=np.float64) if kind=='flux' else np.mean(a,axis=-1,dtype=np.float64)

def weighted_mean(x, area):
    ok=np.isfinite(x)&np.isfinite(area)&(area>0)
    den=float(np.sum(area[ok]))
    return (float(np.sum(x[ok]*area[ok])/den) if den else np.nan,den)

def budget_check(stock,nee,fire,stock_bound,nee_bound,fire_bound,mask,tolerance=.01):
    residual=np.diff(stock,axis=-1)+nee[...,1:]+fire[...,1:]
    bound=stock_bound[...,1:]+stock_bound[...,:-1]+nee_bound[...,1:]+fire_bound[...,1:]+tolerance
    ok=np.asarray(mask)[...,None]&np.isfinite(residual)&np.isfinite(bound)
    return dict(n=int(ok.sum()),failures=int(np.count_nonzero(ok&(np.abs(residual)>bound))),
                maximum_abs=float(np.max(np.abs(residual[ok]))) if np.any(ok) else None,
                maximum_bound=float(np.max(bound[ok])) if np.any(ok) else None)
