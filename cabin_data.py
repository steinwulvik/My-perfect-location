"""UT.no/DNT cabin catalogue with supplementary OpenStreetMap lodging."""
import math
import re
from urllib.parse import urlencode, urlparse, parse_qs
from nearby_data import ENDPOINTS
from ut_cabins import load_ut_catalog, point_from_geojson

LODGING_TYPES = {'alpine_hut', 'wilderness_hut', 'chalet', 'guest_house', 'hostel', 'hotel'}
# Confirmed DNT/UT pages for Oslo/Romerike; only match inside that region.
OSLO_BOOKING_IDS = {'huldreheim': '1015390', 'fiskelaushytta': '10258893',
                    'bøvelstad': '10773', 'røyrivannskoia': '10771',
                    'fjellvang': '10775', 'råbjørnhytta': '10786',
                    'dølerud': '101145', 'fuglemyrhytta': '101209'}


def booking_fields(name, lat, lon, ut_id=None, tags=None):
    tags = tags or {}
    for key in ('website', 'contact:website', 'url', 'booking:website', 'website:booking'):
        for value in str(tags.get(key, '')).split(';'):
            parsed = urlparse(value.strip())
            if parsed.scheme not in ('https', 'http'):
                continue
            if parsed.hostname in ('ut.no', 'www.ut.no', 'hyttebestilling.dnt.no'):
                match = re.match(r'^/hytte/(\d+)(?:/|$)', parsed.path)
                if match:
                    ut_id = ut_id or match[1]
                if parsed.hostname == 'hyttebestilling.dnt.no' and parsed.path == '/kart':
                    params = parse_qs(parsed.query)
                    identity = params.get('value', [''])[0]
                    if params.get('type') == ['cabin'] and identity.isdigit():
                        ut_id = ut_id or identity
    if not ut_id and 59.5 <= lat <= 60.5 and 10 <= lon <= 11.7:
        ut_id = OSLO_BOOKING_IDS.get(normalized_name(name))
    if ut_id and str(ut_id).isdigit():
        return {'utId': str(ut_id), 'utUrl': 'https://ut.no/hytte/'+str(ut_id),
                'bookingUrl': 'https://hyttebestilling.dnt.no/kart?'+urlencode({'type': 'cabin', 'value': str(ut_id)})}
    return {}


def cabin_query(lat, lon, radius):
    types = '|'.join(sorted(LODGING_TYPES))
    return (f'[out:json][timeout:25];nwr(around:{radius},{lat},{lon})'
            f'["tourism"~"^({types})$"];out center;')


def dnt_affiliated(tags):
    # Club attribution, not a DNT discount or a legacy UT database identifier.
    attribution = ' '.join(str(tags.get(key, '')) for key in ('operator', 'owner', 'network', 'brand'))
    if re.search(r'\bdnt\b|turistforening|bergen(?: og hordaland)? turlag', attribution, re.I):
        return True
    return any(re.match(r'https?://(?:[\w-]+\.)*dnt\.no(?:[/:?#]|$)', str(tags.get(key, '')), re.I)
               for key in ('website', 'contact:website', 'operator:website'))


def distance_km(lat, lon, other_lat, other_lon):
    p, q = math.radians(lat), math.radians(other_lat)
    a = math.sin((q-p)/2)**2 + math.cos(p)*math.cos(q)*math.sin(math.radians(other_lon-lon)/2)**2
    return 6371 * 2 * math.asin(math.sqrt(min(1, a)))


def load_osm_cabins(fetch, lat, lon, radius):
    for endpoint in ENDPOINTS:
        try:
            data = fetch(endpoint+'?'+urlencode({'data': cabin_query(lat, lon, radius)}), timeout=35)
            if data.get('remark') or not isinstance(data.get('elements'), list):
                raise RuntimeError('Ufullstendig hytteoppslag')
            cabins = []
            for item in data['elements']:
                tags = item.get('tags', {})
                if tags.get('tourism') not in LODGING_TYPES or not dnt_affiliated(tags):
                    continue
                point = item if item.get('type') == 'node' else item.get('center', {})
                try:
                    x, y = float(point['lat']), float(point['lon'])
                    if not math.isfinite(x) or not math.isfinite(y) or not -90 <= x <= 90 or not -180 <= y <= 180:
                        continue
                    km = distance_km(lat, lon, x, y)
                except (KeyError, TypeError, ValueError):
                    continue
                if km*1000 > radius:
                    continue
                name = tags.get('name') or 'DNT-hytte uten navn'
                # OSM can map a hut as both a node and a building footprint.
                if any(c['name'].casefold() == name.casefold() and distance_km(x, y, c['lat'], c['lon']) < .1 for c in cabins):
                    continue
                if item.get('type') not in ('node', 'way', 'relation') or not str(item.get('id', '')).isdigit():
                    continue
                cabins.append({'name': name, 'lat': x, 'lon': y, 'distanceKm': round(km, 1),
                               'operator': tags.get('operator') or tags.get('owner') or '',
                               'source': 'OpenStreetMap',
                               **booking_fields(name, x, y, tags=tags),
                               'mapUrl': f"https://www.openstreetmap.org/{item['type']}/{item['id']}"})
            return {'cabins': sorted(cabins, key=lambda c: c['distanceKm']), 'radius': radius,
                    'source': 'OpenStreetMap', 'endpoint': endpoint}
        except Exception:
            continue
    raise RuntimeError('Hyttedata kunne ikke hentes fra karttjenerne. Prøv igjen senere.')


def normalized_name(name):
    return re.sub(r'\W+', '', name.casefold())


def same_cabin(a, b):
    return (normalized_name(a['name']) == normalized_name(b['name'])
            and distance_km(a['lat'], a['lon'], b['lat'], b['lon']) < .3)


def load_cabins(fetch, lat, lon, radius, catalog_loader=None):
    errors, cabins, non_dnt = {}, [], []
    ut_ok = osm_ok = False
    try:
        catalog = catalog_loader() if catalog_loader else load_ut_catalog(fetch)
        for row in catalog:
            if not row['dntCabin']:
                # An explicit UT classification beats a stale OSM operator tag.
                try:
                    x, y = point_from_geojson(row.get('geojson'))
                    non_dnt.append({'name': row.get('name') or '', 'lat': x, 'lon': y})
                except (ValueError, TypeError):
                    pass
                continue
            x, y = point_from_geojson(row['geojson'])
            km = distance_km(lat, lon, x, y)
            if km*1000 <= radius:
                cabins.append({'name': row['name'].strip(), 'lat': x, 'lon': y,
                               'distanceKm': round(km, 1), 'operator': '', 'source': 'UT.no/DNT',
                               **booking_fields(row['name'], x, y, ut_id=row['id'])})
        ut_ok = True
    except Exception as exc:
        # No partial UT results after a parsing/catalogue error.
        cabins = []
        errors['ut'] = str(exc)
    try:
        osm = load_osm_cabins(fetch, lat, lon, radius)
        for extra in osm['cabins']:
            if ut_ok and any(same_cabin(extra, private) for private in non_dnt):
                continue
            match = next((hut for hut in cabins if same_cabin(hut, extra)), None)
            if match:
                match['mapUrl'] = extra['mapUrl']
                match['operator'] = extra.get('operator', '')
            else:
                cabins.append(extra)
        osm_ok = True
    except Exception as exc:
        errors['osm'] = str(exc)
    if not ut_ok and not osm_ok:
        raise RuntimeError('Hyttedata kunne ikke hentes fra UT.no eller OpenStreetMap')
    warnings = []
    if not ut_ok:
        warnings.append('UT.no kunne ikke hentes. Viser kun hytter fra OpenStreetMap; listen kan være ufullstendig.')
    if not osm_ok:
        warnings.append('OpenStreetMap kunne ikke hentes. Viser hytter fra UT.no uten supplement.')
    return {'cabins': sorted(cabins, key=lambda hut: (hut['distanceKm'], hut['name'])),
            'radius': radius, 'primaryAvailable': ut_ok,
            'sources': (['UT.no/DNT'] if ut_ok else [])+(['OpenStreetMap'] if osm_ok else []),
            'warnings': warnings, 'errors': errors}
