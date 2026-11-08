"""OpenStreetMap POIs through public, keyless Overpass services."""
from urllib.parse import urlencode
from trail_data import trail_query, normalize_trails

ENDPOINTS=('https://ovp.dresel.systems/api/interpreter','https://overpass-api.de/api/interpreter')


def nearby_query(lat,lon,radius):
    area=f'(around:{radius},{lat},{lon})'
    filters=['["healthcare"~"^(doctor|dentist|pharmacy)$"]',
             '["amenity"~"^(school|kindergarten|college|university|fitness_centre|gym|pharmacy|doctors|dentist|library|cafe|restaurant|charging_station)$"]',
             '["shop"~"^(supermarket|convenience|greengrocer)$"]',
             '["leisure"="fitness_centre"]'
]
    return '[out:json][timeout:25];('+''.join('nwr'+area+f+';' for f in filters)+');out center;'+trail_query(lat,lon,radius)


def load_nearby(fetch,lat,lon,radius):
    query=nearby_query(lat,lon,radius)
    for endpoint in ENDPOINTS:
        try:
            data=fetch(endpoint+'?'+urlencode({'data':query}),timeout=35)
            if data.get('remark') or not isinstance(data.get('elements'),list):
                raise RuntimeError('Ufullstendig kartoppslag')
            return {'elements':normalize_trails(data['elements'],lat,lon),'radius':radius,'source':'OpenStreetMap', 'endpoint':endpoint}
        except Exception:
            continue
    raise RuntimeError('Nærtilbud kunne ikke hentes fra karttjenerne. Prøv igjen senere.')
