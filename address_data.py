"""Incremental Norwegian address lookup via Kartverket's address API."""
import math
from urllib.parse import urlencode


def address_suggestions(fetch, query):
    # Kartverket explicitly forbids combining fuzzy=true with wildcard '*'.
    params = {'sok': query.strip().rstrip('*')+'*', 'treffPerSide': 12, 'side': 0}
    data = fetch('https://ws.geonorge.no/adresser/v1/sok?'+urlencode(params), timeout=12)
    if not isinstance(data.get('adresser'), list):
        raise RuntimeError('Adresseforslag kunne ikke hentes fra Kartverket')
    result, seen = [], set()
    for row in data['adresser']:
        point = row.get('representasjonspunkt') or {}
        try:
            lat, lon = float(point['lat']), float(point['lon'])
            if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        address = row.get('adressetekst') or ''
        if not address:
            continue
        postal = ' '.join(str(row.get(key) or '') for key in ('postnummer', 'poststed')).strip()
        name = ', '.join(filter(None, [address, postal, row.get('kommunenavn')]))
        identity = (name, lat, lon)
        if identity not in seen:
            seen.add(identity)
            result.append({'name': name, 'lat': lat, 'lon': lon, 'type': 'address', 'source': 'Kartverket'})
    return result
