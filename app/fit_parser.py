"""Decode a FIT file into plain Python structures (vendor-neutral).

Uses Garmin's official FIT SDK, which reads Suunto, Garmin, Coros and Polar files alike.
"""
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from garmin_fit_sdk import Decoder, Stream

SEMI_TO_DEG = 180.0 / 2**31


class FitError(ValueError):
    pass


@dataclass
class Parsed:
    start_time: datetime
    sport: str
    sub_sport: str | None
    session: dict
    file_id: dict
    laps: list[dict]
    # per-sample arrays, all the same length
    t: np.ndarray            # epoch seconds (float)
    distance: np.ndarray     # m, monotonic
    lat: np.ndarray          # deg or nan
    lon: np.ndarray
    altitude: np.ndarray     # m or nan
    hr: np.ndarray           # bpm or nan
    speed: np.ndarray        # m/s or nan
    cadence: np.ndarray
    power: np.ndarray
    temperature: np.ndarray
    rr_ms: list[float] = field(default_factory=list)


def _f(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else np.nan


def decode(data: bytes) -> Parsed:
    if len(data) < 14:
        raise FitError("File quá nhỏ, không phải FIT.")
    if not Decoder(Stream.from_byte_array(bytearray(data))).is_fit():
        raise FitError("Không phải file FIT hợp lệ.")
    msgs, errors = Decoder(Stream.from_byte_array(bytearray(data))).read(
        convert_datetimes_to_dates=True, convert_types_to_strings=True,
        expand_components=True, merge_heart_rates=True, apply_scale_and_offset=True,
    )
    recs = msgs.get("record_mesgs") or []
    if not recs:
        msg = f"File FIT lỗi: {errors[0]}" if errors else "File không có dữ liệu bài tập (record)."
        raise FitError(msg)

    rows = [r for r in recs if isinstance(r.get("timestamp"), datetime)]
    rows.sort(key=lambda r: r["timestamp"])
    # drop duplicate timestamps (keep first)
    dedup, last = [], None
    for r in rows:
        ts = r["timestamp"].timestamp()
        if last is None or ts > last:
            dedup.append(r)
            last = ts
    rows = dedup
    if len(rows) < 10:
        raise FitError("Bài tập quá ngắn để phân tích.")

    t = np.array([r["timestamp"].timestamp() for r in rows])
    dist = np.array([_f(r.get("distance")) for r in rows])
    # Fill missing distance forward and force monotonic.
    if np.all(np.isnan(dist)):
        dist = np.zeros(len(rows))
    dist = np.fmax.accumulate(np.nan_to_num(dist, nan=0.0))

    def col(*keys):
        out = np.full(len(rows), np.nan)
        for i, r in enumerate(rows):
            for k in keys:
                v = r.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[i] = v
                    break
        return out

    lat = col("position_lat") * SEMI_TO_DEG
    lon = col("position_long") * SEMI_TO_DEG
    bad = (lat == 0) & (lon == 0)
    lat[bad] = np.nan
    lon[bad] = np.nan
    hr = col("heart_rate")
    hr[(hr < 30) | (hr > 240)] = np.nan

    sessions = msgs.get("session_mesgs") or [{}]
    sess = sessions[0]
    fid = (msgs.get("file_id_mesgs") or [{}])[0]
    start = sess.get("start_time") if isinstance(sess.get("start_time"), datetime) else rows[0]["timestamp"]
    sub = sess.get("sub_sport")
    rr: list[float] = []
    for h in msgs.get("hrv_mesgs") or []:
        for v in h.get("time") or []:
            if isinstance(v, (int, float)) and 0.2 < v < 3.0:  # seconds
                rr.append(round(v * 1000, 1))

    return Parsed(
        start_time=start,
        sport=str(sess.get("sport") or "generic"),
        sub_sport=str(sub) if sub and sub != "generic" else None,
        session=sess,
        file_id=fid,
        laps=msgs.get("lap_mesgs") or [],
        t=t, distance=dist, lat=lat, lon=lon,
        altitude=col("enhanced_altitude", "altitude"),
        hr=hr,
        speed=col("enhanced_speed", "speed"),
        cadence=col("cadence"),
        power=col("power"),
        temperature=col("temperature"),
        rr_ms=rr,
    )
