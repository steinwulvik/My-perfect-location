from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json
import math
import ssl
import certifi
import time
import socket
import sys
import hashlib
import os
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

ROOT = Path(__file__).resolve().parent
SERVER_REVISION = hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in sorted(ROOT.glob('*.py')))).hexdigest()[:16]
TLS = ssl.create_default_context(cafile=certifi.where())
CACHE = {}
CACHE_MAX_ENTRIES = 4096
CACHE_MAX_BYTES = 64 * 1024 * 1024
CACHE_MAX_ENTRY_BYTES = 8 * 1024 * 1024
CACHE_LAST_SWEEP = 0
CACHE_LOCK = RLock()
CACHE_KEY_LOCKS = [RLock() for _ in range(64)]
APP_AGENT = "MyPerfectLocation/0.1 (local learning project)"
NVE_LAKES = "https://kart.nve.no/enterprise/rest/services/Innsjodatabase2/MapServer/5/query"
NVE_FLOODS = "https://gis3.nve.no/map/rest/services/Mapservices/FlomHendelser/MapServer/0/query"
NVE_FLOOD_OBSERVATIONS = "https://gis3.nve.no/map/rest/services/Mapservices/FlomHendelser/MapServer/1/query"
NVE_FLOOD_AREAS = "https://gis3.nve.no/map/rest/services/Mapservices/Flomhendelser1/MapServer/1/query"
NVE_CLAY = "https://gis3.nve.no/arcgis/rest/services/wmts/Kvikkleire_Jordskred_bkp/MapServer/1/query"
NVE_LANDSLIDE = "https://gis3.nve.no/arcgis/rest/services/wmts/Kvikkleire_Jordskred/MapServer/3/query"
NVE_SNOW = "https://kart.nve.no/enterprise/rest/services/SnoskredAktsomhet/MapServer"
KARTVERKET_HEIGHT = "https://ws.geonorge.no/hoydedata/v1/punkt"
NGU_RADON = "https://geo.ngu.no/mapserver/RadonUranAktsomhetWMS"
GBIF_DATASET = "a639542a-654a-427b-9cf1-bde1953bbb52"


def fetch_json(url, method="GET", data=None, headers=None, timeout=35):
    h = {"User-Agent": APP_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        h.setdefault("Content-Type", "application/json; charset=utf-8")
    req = Request(url, data=body, headers=h, method=method)
    with urlopen(req, timeout=timeout, context=TLS) as response:
        return json.loads(response.read().decode("utf-8"))


def cache_size(value):
    """Conservative per-entry object size; shared objects count once per entry."""
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if total > CACHE_MAX_ENTRY_BYTES:
            return total
        if isinstance(item, dict):
            pending.extend(item.keys()); pending.extend(item.values())
        elif isinstance(item, (list, tuple, set)):
            pending.extend(item)
    return total


def cached_json(key, ttl, loader):
    global CACHE_LAST_SWEEP
    def failed(value):
        return isinstance(value, dict) and bool(value.get("error") or value.get("errors") or value.get("remark"))
    with CACHE_KEY_LOCKS[hash(key) % len(CACHE_KEY_LOCKS)]:
        now = time.time()
        with CACHE_LOCK:
            if now - CACHE_LAST_SWEEP > 30:
                for expired in [k for k, item in CACHE.items() if item[0] <= now]:
                    CACHE.pop(expired, None)
                CACHE_LAST_SWEEP = now
            item = CACHE.pop(key, None)
            if item and item[0] > now and not failed(item[1]):
                CACHE[key] = item  # Move a hit to the most recently used end.
                return item[1]
        result = loader()
        # Never retain upstream error/partial results. Concurrent identical
        # requests share a successful load, without blocking unrelated keys.
        size = cache_size(result) if not failed(result) else 0
        if not failed(result) and size <= CACHE_MAX_ENTRY_BYTES and ttl > 0:
            with CACHE_LOCK:
                used = sum(item[2] if len(item) > 2 else cache_size(item[1]) for item in CACHE.values())
                while CACHE and (len(CACHE) >= CACHE_MAX_ENTRIES or used + size > CACHE_MAX_BYTES):
                    removed = CACHE.pop(next(iter(CACHE)))
                    used -= removed[2] if len(removed) > 2 else cache_size(removed[1])
                CACHE[key] = (time.time() + ttl, result, size)
        return result


def radon_at_point(lat, lon):
    # WMS 1.1.1 uses longitude/latitude order for EPSG:4326.
    params = {"service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
              "layers": "Radon_aktsomhet", "query_layers": "Radon_aktsomhet",
              "styles": "", "srs": "EPSG:4326",
              "bbox": f"{lon-0.0001},{lat-0.0001},{lon+0.0001},{lat+0.0001}",
              "width": "101", "height": "101", "x": "50", "y": "50",
              "info_format": "application/vnd.ogc.gml", "feature_count": "1"}
    req = Request(NGU_RADON + "?" + urlencode(params), headers={"User-Agent": APP_AGENT})
    with urlopen(req, timeout=25, context=TLS) as response:
        root = ET.fromstring(response.read())
    fields = {node.tag.split("}")[-1]: (node.text or "").strip() for node in root.iter()}
    if "ServiceException" in fields:
        raise RuntimeError("NGU kunne ikke gjennomføre radonoppslaget.")
    label = fields.get("radonAktsomhetGradTekst")
    grade = (label or "").lower()
    level = "red" if "høy" in grade else "orange" if any(x in grade for x in ("middels", "moderat")) else "green" if "lav" in grade else "unknown"
    return {"label": label or "Ikke kartlagt / ukjent", "level": level,
            "source": "NGU og DSA · Nasjonalt aktsomhetskart for radon, 2026"}


class InvalidCoordinates(ValueError):
    pass


def coordinates(params):
    try:
        lat = float(params.get("lat", [""])[0])
        lon = float(params.get("lon", [""])[0])
    except (ValueError, TypeError, IndexError):
        raise InvalidCoordinates("Koordinatene er ugyldige.") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise InvalidCoordinates("Koordinatene er ugyldige.")
    return lat, lon


def arcgis_point_params(lat, lon, distance, extra):
    fields = {
        "f": "json",
        "where": "1=1",
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}, separators=(",", ":")),
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": str(distance),
        "units": "esriSRUnit_Meter",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "100",
    }
    fields.update(extra)
    return fields


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def geom_center(geometry):
    if not geometry:
        return None
    points = []
    for ring in geometry.get("rings", []):
        points.extend(ring)
    if not points:
        for path in geometry.get("paths", []):
            points.extend(path)
    if not points:
        return None
    return (sum(p[1] for p in points) / len(points), sum(p[0] for p in points) / len(points))


def flood_event_date(attrs):
    """Return (date, display text) from the date fields exposed by NVE."""
    raw = attrs.get("startDato") or attrs.get("datoTekst") or attrs.get("dato")
    if raw is not None:
        value = str(raw).strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(value[:19], fmt).date()
                return parsed, parsed.strftime("%d.%m.%Y")
            except ValueError:
                pass
        try:
            stamp = float(raw)
            if stamp > 10_000_000_000:
                stamp /= 1000
            parsed = datetime.fromtimestamp(stamp, timezone.utc).date()
            return parsed, parsed.strftime("%d.%m.%Y")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    year = attrs.get("aar")
    try:
        parsed = date(int(year), 1, 1)
        return parsed, str(year)
    except (TypeError, ValueError):
        return None, "Dato ikke oppgitt"


def point_in_rings(lat, lon, rings):
    inside = False
    for ring in rings:
        ring_inside = False
        for i, point in enumerate(ring):
            previous = ring[i - 1]
            x1, y1 = point[0], point[1]
            x2, y2 = previous[0], previous[1]
            if ((y1 > lat) != (y2 > lat)) and lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-15) + x1:
                ring_inside = not ring_inside
        if ring_inside:
            inside = not inside
    return inside


def nearest_polygon_point(lat, lon, geometries):
    if not geometries:
        return None
    # Always sample the event polygon boundary. Using the address itself when
    # the point lies inside a broad event polygon makes the height difference
    # zero by definition and defeats the elevation filter.
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    best = None
    best_distance = float("inf")
    for geometry in geometries:
        for ring in geometry.get("rings", []):
            for i, a in enumerate(ring):
                b = ring[(i + 1) % len(ring)]
                ax, ay = (a[0] - lon) * 111320 * cos_lat, (a[1] - lat) * 111320
                bx, by = (b[0] - lon) * 111320 * cos_lat, (b[1] - lat) * 111320
                dx, dy = bx - ax, by - ay
                denom = dx * dx + dy * dy
                t = max(0.0, min(1.0, -(ax * dx + ay * dy) / denom)) if denom else 0.0
                px, py = ax + t * dx, ay + t * dy
                distance = px * px + py * py
                if distance < best_distance:
                    best_distance = distance
                    best = (lat + py / 111320, lon + px / (111320 * cos_lat))
    return best


def nearest_flood_observation(lat, lon, geometries):
    points = []
    for geometry in geometries:
        if "x" in geometry and "y" in geometry:
            points.append((geometry["y"], geometry["x"]))
        elif geometry.get("rings"):
            # These are mapped inundation areas, never regional event outlines.
            if point_in_rings(lat, lon, geometry["rings"]):
                return (lat, lon)
            point = nearest_polygon_point(lat, lon, [geometry])
            if point:
                points.append(point)
    return min(points, key=lambda p: haversine(lat, lon, *p)) if points else None


def local_flood_features(lat, lon):
    features = []
    for url, kind in ((NVE_FLOOD_OBSERVATIONS, "point"), (NVE_FLOOD_AREAS, "area")):
        params = arcgis_point_params(lat, lon, 100, {"resultRecordCount": "1000"})
        data = fetch_json(url + "?" + urlencode(params), timeout=45)
        if "error" in data or data.get("exceededTransferLimit"):
            raise RuntimeError("NVE kunne ikke levere et fullstendig lokalt flomoppslag.")
        for feature in data.get("features", []):
            a = feature.get("attributes", {})
            if kind == "point":
                attrs = {"flomhendelseId": a.get("FlomhendelseId"), "startDato": a.get("Maletid"), "navn": "Registrert flomvannstand"}
            else:
                # This layer records observed flood extent; datafangstdato is the observation date.
                attrs = {"startDato": a.get("datafangstdato"), "navn": a.get("flomnavn") or a.get("navn") or "Registrert flomareal"}
            features.append({"attributes": attrs, "geometry": feature.get("geometry")})
    ids = {int(f["attributes"]["flomhendelseId"]) for f in features if f["attributes"].get("flomhendelseId")}
    if ids:
        # Use the latest event catalogue for dates/names only, not its regional polygons.
        params = {"f": "json", "where": "flomhendelseId IN (" + ",".join(map(str, sorted(ids))) + ")", "outFields": "flomhendelseId,startDato,navn", "returnGeometry": "false"}
        data = fetch_json(NVE_FLOODS + "?" + urlencode(params), timeout=35)
        if "error" in data or data.get("exceededTransferLimit"):
            raise RuntimeError("Dato for den lokale flomobservasjonen kunne ikke kontrolleres.")
        metadata = {f["attributes"]["flomhendelseId"]: f["attributes"] for f in data.get("features", [])}
        for feature in features:
            feature["attributes"].update(metadata.get(feature["attributes"].get("flomhendelseId"), {}))
    return features


def kartverket_heights(points):
    """Get terrain heights for (lat, lon) pairs; Kartverket accepts up to 50 per call."""
    values = []
    for start in range(0, len(points), 50):
        batch = points[start:start + 50]
        encoded_points = json.dumps([[lon, lat] for lat, lon in batch], separators=(",", ":"))
        url = KARTVERKET_HEIGHT + "?" + urlencode({"koordsys": 4258, "punkter": encoded_points})
        data = fetch_json(url, timeout=35)
        returned = data.get("punkter", [])
        for i in range(len(batch)):
            try:
                values.append(float(returned[i]["z"]))
            except (IndexError, KeyError, TypeError, ValueError):
                values.append(None)
    return values


def unique_flood_events(features, lat, lon):
    by_id = {}
    for feature in features:
        attrs = feature.get("attributes", {})
        event_id = attrs.get("flomhendelseId") or attrs.get("flomID") or attrs.get("hovedhendelseID") or attrs.get("OBJECTID")
        key = str(event_id) if event_id not in (None, "", " ") else json.dumps(attrs, sort_keys=True, ensure_ascii=False)
        event = by_id.setdefault(key, {"attributes": attrs, "geometries": []})
        if len(attrs) > len(event["attributes"]):
            event["attributes"] = attrs
        if feature.get("geometry"):
            event["geometries"].append(feature["geometry"])
    events = []
    for event in by_id.values():
        attrs = event["attributes"]
        event_date, date_label = flood_event_date(attrs)
        event_type = next((str(attrs.get(field)).strip() for field in ("hendelse", "flomNavn", "navn", "arsak") if attrs.get(field) not in (None, "", " ")), "Flomhendelse")
        nearest = nearest_flood_observation(lat, lon, event["geometries"])
        if nearest is None or haversine(lat, lon, *nearest) * 1000 > 100:
            continue
        if event_date is None:
            raise RuntimeError("En lokal flomobservasjon mangler dato og kan ikke vurderes.")
        events.append({"date": event_date, "dateLabel": date_label, "type": event_type, "nearestPoint": nearest, "height": None})
    address_height = None
    height_unknown = 0
    excluded_by_height = 0
    if events:
        try:
            points = [(lat, lon)] + [event["nearestPoint"] for event in events if event["nearestPoint"]]
            heights = kartverket_heights(points)
            address_height = heights[0] if heights else None
            height_index = 1
            for event in events:
                if event["nearestPoint"]:
                    event["height"] = heights[height_index] if height_index < len(heights) else None
                    height_index += 1
                if address_height is not None and event["height"] is not None:
                    event["heightDifference"] = address_height - event["height"]
                else:
                    event["heightDifference"] = None
                    height_unknown += 1
        except Exception:
            height_unknown = len(events)
        if height_unknown:
            raise RuntimeError("Høydekontrollen for lokale flomobservasjoner kunne ikke fullføres.")
        kept = []
        for event in events:
            # Count only events whose elevation comparison succeeded and is within 5 m.
            # Unknown heights cannot be treated as a passed height check.
            difference = event.get("heightDifference")
            if difference is None:
                continue
            if difference > 5:
                excluded_by_height += 1
            else:
                kept.append(event)
        events = kept
    events.sort(key=lambda item: item["date"] or date.min)
    today = date.today()
    cutoff = date(today.year - 10, today.month, min(today.day, 28) if today.month == 2 else today.day)
    recent = [event for event in events if event["date"] and cutoff <= event["date"] <= today]
    serialize = lambda event: {"type": event["type"], "date": event["dateLabel"]}
    return {
        "historicalCount": len(events),
        "recentCount": len(recent),
        "lastHistorical": serialize(events[-1]) if events else None,
        "lastRecent": serialize(recent[-1]) if recent else None,
        "cutoffDate": cutoff.isoformat(),
        "withinMeters": 100,
        "excludedByElevation": excluded_by_height,
        "heightUnknownCount": height_unknown,
        "addressElevation": address_height,
        "source": "Stedfestet flomvannstand og registrert flomareal fra NVE; regionale hendelsesomrader telles ikke.",
    }


def load_nearby_combined(lat, lon, radius):
    from nearby_data import load_nearby
    from transit_data import load_transit_stops
    result = {'elements': [], 'errors': {}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {'osm': pool.submit(load_nearby, fetch_json, lat, lon, radius),
                'transit': pool.submit(load_transit_stops, fetch_json, lat, lon, radius)}
        for source, job in jobs.items():
            try:
                data = job.result()
                result['elements'].extend(data['elements'] if source == 'osm' else data)
            except Exception as exc:
                result['errors'][source] = str(exc)
    if len(result['errors']) == 2:
        raise RuntimeError('Nærtilbud kunne ikke hentes fra OpenStreetMap eller Entur')
    return result


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                return self.send_json(200, {"app": "my-perfect-location", "status": "ok",
                                           "revision": SERVER_REVISION, "features": ["cabins-ut-osm", "nearby-trails", "routes", "area-search"]})
            if parsed.path == "/":
                return self.send_file("index.html")
            if parsed.path.startswith('/api/area-'):
                from area_search import municipalities, candidates, context, places, place_candidates
                try:
                    if parsed.path == '/api/area-municipalities':
                        return self.send_json(200, cached_json('area-municipalities-v1', 86400,
                            lambda: municipalities(fetch_json)))
                    if parsed.path == '/api/area-places':
                        code = q.get('municipality', [''])[0]
                        return self.send_json(200, cached_json('area-places-v3:'+code, 86400,
                            lambda: places(fetch_json, code)))
                    if parsed.path == '/api/area-place-candidates':
                        code, place_id = q.get('municipality', [''])[0], q.get('place', [''])[0]
                        radius = float(q.get('scopeRadius', ['3'])[0])
                        if not place_id.isdigit() or not .5 <= radius <= 20:
                            raise ValueError('Velg sted og en områderadius mellom 0,5 og 20 km.')
                        catalog = cached_json('area-places-v3:'+code, 86400, lambda: places(fetch_json, code))
                        place = next((p for p in catalog if p['id'] == place_id), None)
                        if place is None:
                            raise ValueError('Stedet finnes ikke i valgt kommune. Velg sted på nytt.')
                        postal, street = q.get('postalCode', [''])[0].strip(), q.get('street', [''])[0].strip()
                        key = 'area-place-addresses-v3:'+json.dumps([code, place_id, radius, postal, street.casefold()])
                        data = cached_json(key, 3600, lambda: place_candidates(fetch_json, code, place, radius, postal, street))
                        if not data['complete']:
                            with CACHE_LOCK:
                                CACHE.pop(key, None)
                        return self.send_json(200, data)
                    if parsed.path == '/api/area-candidates':
                        code = q.get('municipality', [''])[0]
                        locality = q.get('locality', [''])[0].strip()
                        postal_code = q.get('postalCode', [''])[0].strip()
                        street = q.get('street', [''])[0].strip()
                        key = 'area-addresses-v2:' + json.dumps([code, locality.casefold(), postal_code, street.casefold()])
                        # Incomplete datasets must not be retained as successful full searches.
                        data = cached_json(key, 3600, lambda: candidates(fetch_json, code, locality, postal_code, street))
                        if not data['complete']:
                            with CACHE_LOCK:
                                CACHE.pop(key, None)
                        return self.send_json(200, data)
                    if parsed.path == '/api/area-context':
                        lat, lon = coordinates(q)
                        radius = float(q.get('radius', [''])[0])
                        if not 1 <= radius <= 100:
                            raise ValueError('Søkeradius må være mellom 1 og 100 km.')
                        flags = tuple(q.get(k, ['0'])[0] == '1' for k in ('nearby', 'fish', 'bath'))
                        return self.send_json(200, cached_json(f'area-context-v1:{lat:.5f}:{lon:.5f}:{radius}:{flags}', 900,
                            lambda: context(fetch_json, lat, lon, radius, *flags,
                                (arcgis_point_params, nearest_polygon_point, point_in_rings, haversine))))
                except ValueError as exc:
                    return self.send_json(400, {'error': str(exc)})
            if parsed.path == "/api/address-suggestions":
                from address_data import address_suggestions
                query = q.get('q', [''])[0].strip()
                if len(query) < 3:
                    return self.send_json(200, [])
                if len(query) > 160:
                    return self.send_json(400, {'error': 'Adressesøket er for langt.'})
                return self.send_json(200, cached_json('address-suggest-v2:'+query.casefold(), 86400,
                                                      lambda: address_suggestions(fetch_json, query)))
            if parsed.path == "/api/route":
                from route_data import load_route
                lat, lon = coordinates(q)
                to_lat, to_lon = coordinates({'lat': q.get('toLat', ['']), 'lon': q.get('toLon', [''])})
                mode = q.get('mode', [''])[0]
                if mode not in ('walking', 'bicycling', 'driving', 'transit'):
                    return self.send_json(400, {'error': 'Velg en gyldig transportmetode.'})
                ttl = 60 if mode == 'transit' else 900
                key = f'route-v2:{lat:.6f}:{lon:.6f}:{to_lat:.6f}:{to_lon:.6f}:{mode}'
                return self.send_json(200, cached_json(key, ttl, lambda: load_route(fetch_json, lat, lon, to_lat, to_lon, mode)))
            if parsed.path == "/api/geocode":
                name = q.get("q", [""])[0].strip()
                if not name or len(name) > 160:
                    return self.send_json(400, {"error": "Skriv inn et stedsnavn eller en adresse."})
                url = "https://nominatim.openstreetmap.org/search?" + urlencode({"q": name, "format": "jsonv2", "limit": 6, "countrycodes": "no", "addressdetails": 1})
                rows = cached_json("geo:" + name.lower(), 86400, lambda: fetch_json(url, headers={"User-Agent": APP_AGENT}, timeout=25))
                places = []
                seen = set()
                for x in rows:
                    lat, lon = float(x["lat"]), float(x["lon"])
                    identity = (round(lat, 3), round(lon, 3), x.get("display_name", "").casefold())
                    if any(item[2] == identity[2] for item in seen):
                        continue
                    if identity in seen:
                        continue
                    seen.add(identity)
                    places.append({"name": x.get("display_name", name), "lat": lat, "lon": lon, "type": x.get("type", "")})
                return self.send_json(200, places)
            if parsed.path == "/api/cabins":
                from cabin_data import load_cabins
                from ut_cabins import load_ut_catalog, UT_GRAPHQL
                lat, lon = coordinates(q)
                try:
                    radius = int(q.get("radius", ["25000"])[0])
                    if not 1000 <= radius <= 100000:
                        raise ValueError()
                except ValueError:
                    return self.send_json(400, {"error": "Søkeradius for hytter må være mellom 1 og 100 km."})
                data = cached_json(f"cabins-v2:{lat:.6f}:{lon:.6f}:{radius}", 900,
                                   lambda: load_cabins(fetch_json, lat, lon, radius,
                                       catalog_loader=lambda: cached_json('ut-catalog-v1:'+UT_GRAPHQL, 3600,
                                                                          lambda: load_ut_catalog(fetch_json))))
                return self.send_json(200, data)
            if parsed.path == "/api/nearby":
                from nearby_data import load_nearby
                lat, lon = coordinates(q)
                try:
                    radius = int(q.get("radius", ["5000"])[0])
                    if not 1000 <= radius <= 100000:
                        raise ValueError()
                except ValueError:
                    return self.send_json(400, {"error":"Søkeradius for nærtilbud må være mellom 1 og 100 km."})
                data = cached_json(f"near-v6:{lat:.6f}:{lon:.6f}:{radius}", 900,
                                   lambda: load_nearby_combined(lat,lon,radius))
                return self.send_json(200, data)
            if parsed.path == "/api/weather":
                lat, lon = coordinates(q)
                args = {"latitude": lat, "longitude": lon, "start_date": "1991-01-01", "end_date": "2020-12-31", "daily": "temperature_2m_mean,precipitation_sum,snowfall_sum,wind_speed_10m_max,sunshine_duration", "timezone": "Europe/Oslo", "wind_speed_unit": "ms"}
                url = "https://archive-api.open-meteo.com/v1/archive?" + urlencode(args)
                data = cached_json(f"weather:{lat:.3f}:{lon:.3f}", 86400, lambda: fetch_json(url, timeout=55))
                return self.send_json(200, data)
            if parsed.path == "/api/fish-info":
                from fish_info import wikipedia_fish
                species = q.get("species", [""])[0].strip()
                if not species or len(species) > 120:
                    return self.send_json(400, {"error": "Ugyldig fisketype."})
                data = cached_json("fish-info:" + species, 86400, lambda: wikipedia_fish(fetch_json, species))
                return self.send_json(200, data)
            if parsed.path == "/api/waters":
                from water_data import load_waters
                lat, lon = coordinates(q)
                try:
                    fish_radius = float(q.get("fishRadius", ["5"])[0])
                    bath_radius = float(q.get("bathRadius", ["5"])[0])
                    if not (1 <= fish_radius <= 100 and 1 <= bath_radius <= 100):
                        raise ValueError()
                except ValueError:
                    return self.send_json(400, {"error":"Søkeradius må være mellom 1 og 100 km."})
                data = cached_json(f"waters-v2:{lat:.5f}:{lon:.5f}:{fish_radius}:{bath_radius}", 1800, lambda: load_waters(
                    fetch_json, lat, lon, arcgis_point_params, nearest_polygon_point, point_in_rings, haversine,
                    radius_km=fish_radius, bath_radius_km=bath_radius))
                return self.send_json(200, data)
            if parsed.path == "/api/lake":
                lat, lon = coordinates(q)
                distance = max(1000, min(25000, int(q.get("radius", ["15000"])[0])))
                def load_lakes():
                    url = NVE_LAKES + "?" + urlencode(arcgis_point_params(lat, lon, distance, {"returnGeometry": "true", "outFields": "navn,vatnlnr,areal_km2,hoyde,kommune", "resultRecordCount": "100"}))
                    data = fetch_json(url, timeout=45)
                    features = []
                    for f in data.get("features", []):
                        center = geom_center(f.get("geometry"))
                        if center:
                            attrs = f.get("attributes", {})
                            lake_id = attrs.get("vatnlnr") or attrs.get("vatnLnr") or attrs.get("OBJECTID")
                            features.append({"name": attrs.get("navn") or attrs.get("NAVN") or attrs.get("innsjonavn") or (f"Navnløst vann · ID {lake_id}" if lake_id else "Navnløst vann"), "id": lake_id, "lat": center[0], "lon": center[1], "distanceKm": round(haversine(lat, lon, center[0], center[1]), 1), "attributes": attrs})
                    return sorted(features, key=lambda x: x["distanceKm"])[:15]
                lakes = cached_json(f"lake:{lat:.4f}:{lon:.4f}", 86400, load_lakes)
                return self.send_json(200, lakes)
            if parsed.path == "/api/fish":
                lat, lon = coordinates(q)
                radius = max(1000, min(20000, int(q.get("radius", ["5000"])[0])))
                dlat = radius / 111000
                dlon = radius / max(1000, 111000 * math.cos(math.radians(lat)))
                polygon = f"POLYGON(({lon-dlon} {lat-dlat},{lon+dlon} {lat-dlat},{lon+dlon} {lat+dlat},{lon-dlon} {lat+dlat},{lon-dlon} {lat-dlat}))"
                url = "https://api.gbif.org/v1/occurrence/search?" + urlencode({"datasetKey": GBIF_DATASET, "geometry": polygon, "limit": 100, "hasCoordinate": "true"})
                data = cached_json(f"fish:{lat:.3f}:{lon:.3f}:{radius}", 86400, lambda: fetch_json(url, timeout=50))
                records = []
                for x in data.get("results", []):
                    if x.get("decimalLatitude") is None or x.get("decimalLongitude") is None:
                        continue
                    records.append({"species": x.get("species") or x.get("scientificName") or "Ukjent art", "vernacularName": x.get("vernacularName"), "locality": x.get("locality") or x.get("waterBody") or "Lokalitet ikke oppgitt", "date": x.get("eventDate") or x.get("year"), "lat": x["decimalLatitude"], "lon": x["decimalLongitude"], "distanceKm": round(haversine(lat, lon, x["decimalLatitude"], x["decimalLongitude"]), 1), "recordUrl": "https://www.gbif.org/occurrence/" + str(x.get("key"))})
                records.sort(key=lambda x: x["distanceKm"])
                return self.send_json(200, {"records": records[:30], "total": data.get("count", 0), "source": "NINA Vanndata fisk via GBIF"})
            if parsed.path == "/api/hazards":
                lat, lon = coordinates(q)
                def get_floods():
                    return unique_flood_events(local_flood_features(lat, lon), lat, lon)
                def get_clay():
                    # Only count mapped quick-clay objects that intersect the 10 m search radius.
                    url = NVE_CLAY + "?" + urlencode(arcgis_point_params(lat, lon, 10, {"returnGeometry": "false", "returnCountOnly": "false", "outFields": "risiko,skredOmrID,skredOmrNavn", "resultRecordCount": "100"}))
                    data = fetch_json(url, timeout=45)
                    if "error" in data:
                        raise RuntimeError(data["error"].get("message", "NVE-oppslag feilet"))
                    features = data.get("features", [])
                    classes = []
                    for feature in features:
                        value = feature.get("attributes", {}).get("risiko")
                        try:
                            risk_class = int(value)
                            if 1 <= risk_class <= 5:
                                classes.append(risk_class)
                        except (TypeError, ValueError):
                            pass
                    return {"count": len(features), "riskClass": max(classes) if classes else None, "withinMeters": 10}
                def count_nve_layer(url, extra=None):
                    params = arcgis_point_params(lat, lon, 10, {"returnGeometry": "false", "returnCountOnly": "true"})
                    if extra:
                        params.update(extra)
                    data = fetch_json(url + "?" + urlencode(params), timeout=45)
                    if "error" in data:
                        raise RuntimeError(data["error"].get("message", "NVE-oppslag feilet"))
                    return int(data.get("count", 0))
                def get_landslide():
                    return {"count": count_nve_layer(NVE_LANDSLIDE), "withinMeters": 10}
                def get_snow():
                    # Include both the current S2 screening layer and the combined S3 layer.
                    counts = [count_nve_layer(f"{NVE_SNOW}/{layer}/query") for layer in (1, 2)]
                    return {"count": max(counts), "withinMeters": 10}
                results = {}
                errors = {}
                for name, fn in (("floods", get_floods), ("clay", get_clay), ("landslide", get_landslide), ("snow", get_snow), ("radon", lambda: radon_at_point(lat, lon))):
                    try:
                        results[name] = cached_json(f"{name}:{lat:.6f}:{lon:.6f}", 3600, fn)
                    except Exception as exc:
                        results[name] = None
                        errors[name] = str(exc)
                return self.send_json(200, {**results, "errors": errors})
            return self.send_json(404, {"error": "Fant ikke denne adressen i appen."})
        except InvalidCoordinates as exc:
            self.send_json(400, {"error": str(exc)})
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            self.send_json(502, {"error": f"Datakilden svarte {exc.code}: {detail}"})
        except (URLError, TimeoutError) as exc:
            self.send_json(502, {"error": f"Kunne ikke nå datakilden: {exc}"})
        except Exception as exc:
            self.send_json(502, {"error": str(exc)})

    def send_file(self, name):
        try:
            body = (ROOT / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"{self.log_date_time_string()} - {fmt % args}")


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can allow multiple servers on the same port.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    if '--revision' in sys.argv:
        print(SERVER_REVISION)
        sys.exit(0)
    render_runtime = bool(os.environ.get("PORT") or os.environ.get("RENDER"))
    host = os.environ.get("HOST", "0.0.0.0" if render_runtime else "127.0.0.1")
    port = int(os.environ.get("PORT", "8003"))
    try:
        server = LocalHTTPServer((host, port), Handler)
    except OSError as exc:
        if host == "127.0.0.1" and port == 8003 and (exc.errno == 98 or getattr(exc, "winerror", None) == 10048):
            try:
                with urlopen("http://127.0.0.1:8003/api/health", timeout=3) as response:
                    running = json.load(response)
                if running.get("app") == "my-perfect-location":
                    if running.get('revision') != SERVER_REVISION:
                        print('En eldre appserver kjører. Bruk "Start app.cmd" for å oppdatere den automatisk.')
                        sys.exit(1)
                    print("My Perfect Location kjører allerede. Åpne http://127.0.0.1:8003")
                    sys.exit(0)
            except (URLError, TimeoutError, ValueError):
                pass
            print("Port 8003 er i bruk. En server kan allerede kjøre på http://127.0.0.1:8003")
            sys.exit(1)
        raise
    if host == "127.0.0.1":
        print("My Perfect Location kjører lokalt på http://127.0.0.1:8003")
        print("Stopp serveren med Ctrl+C")
    else:
        print(f"My Perfect Location lytter på {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
