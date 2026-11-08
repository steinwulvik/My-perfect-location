"""Nearby mapped waters and bathing places. No inferred fishing permissions."""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse
import math
import json
from pathlib import Path
from datetime import date

SPECIES = {'Salmo trutta': 'Ørret', 'Salmo salar': 'Laks', 'Perca fluviatilis': 'Abbor',
           'Esox lucius': 'Gjedde', 'Rutilus rutilus': 'Mort', 'Carassius carassius': 'Karuss',
           'Coregonus lavaretus': 'Sik', 'Salvelinus alpinus': 'Røye', 'Thymallus thymallus': 'Harr',
           'Lota lota': 'Lake', 'Anguilla anguilla': 'Ål', 'Abramis brama': 'Brasme',
           'Scardinius erythrophthalmus': 'Sørv', 'Phoxinus phoxinus': 'Ørekyt'}


def safe_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in ('https', 'http') and parsed.netloc and not parsed.username else None


def approved_access(water_id, registry):
    evidence = registry.get(str(water_id), {})
    try:
        age = (date.today() - date.fromisoformat(evidence['checkedOn'])).days
    except (ValueError, KeyError, TypeError):
        return None
    if not 0 <= age <= 180 or evidence.get('drinkingWaterStatus') in ('drinking_water', 'in_use', 'registered'):
        return None
    if not all(safe_url(evidence.get(key)) for key in ('fishPresenceSource','permitUrl','permitSource')):
        return None
    return evidence


def load_waters(fetch, lat, lon, point_params, nearest_point, contains, distance, access_registry=None, radius_km=10, bath_radius_km=10, include_species=True, include_geometry=False, include_fishing=True, include_bathing=True):
    discovery_errors = {}
    if not include_fishing:
        access_registry = {}
    elif access_registry is None:
        access_registry = json.loads(Path(__file__).with_name('fishing_access.json').read_text(encoding='utf-8'))
        from fishing_permits import discover_registry
        discovered = discover_registry(fetch, lat, lon, radius_km, point_params, nearest_point, distance, failures=discovery_errors)
        # Preserve known drinking-water exclusions and manually checked facts.
        access_registry = {**discovered, **access_registry}
    radius = radius_km * 1000
    bath_radius = bath_radius_km * 1000
    lake_url = 'https://kart.nve.no/enterprise/rest/services/Innsjodatabase2/MapServer/5/query'
    def all_features(url, params):
        features, offset, seen = [], 0, set()
        for page in range(100):
            data = fetch(url + '?' + urlencode({**params, 'resultOffset':offset,
                         'resultRecordCount':1000, 'orderByFields':'OBJECTID ASC'}), timeout=35)
            if 'error' in data:
                raise RuntimeError('Kartoppslaget feilet.')
            batch = data.get('features', [])
            signature = json.dumps(batch, sort_keys=True)
            if batch and signature in seen:
                raise RuntimeError('Karttjeneren gjentar samme resultatside.')
            seen.add(signature)
            features.extend(batch)
            if not data.get('exceededTransferLimit'):
                return features
            if not batch:
                raise RuntimeError('Kartoppslaget returnerte en ufullstendig resultatside.')
            offset += len(batch)
        raise RuntimeError('Kartoppslaget inneholder for mange resultater.')
    def lakes():
        ids = [str(int(key)) for key in access_registry if key.isdigit() and approved_access(key, access_registry)]
        if not ids:
            return []
        # Query only verified candidates. A large radius must not download every
        # lake polygon in the region before applying the same eligibility rule.
        return all_features(lake_url, point_params(lat, lon, radius, {
            'outFields':'navn,vatnlnr,areal_km2,kommune', 'where':'vatnlnr IN (' + ','.join(ids) + ')'}))
    def places():
        base = 'https://testarcgis02.miljodirektoratet.no/arcgis/rest/services/Badeplasser/Badeplasser_status/MapServer/1'
        params = point_params(lat, lon, bath_radius, {'outFields': 'OBJECTID,Navn,Kommunenavn', 'resultRecordCount': '1000'})
        elements = []
        for feature in all_features(base + '/query', params):
            point = nearest_point(lat, lon, [feature.get('geometry') or {}])
            if not point:
                continue
            attrs = feature.get('attributes') or {}
            record_url = base + '/query?' + urlencode({'f':'html', 'where':f'OBJECTID={int(attrs["OBJECTID"])}', 'outFields':'*'})
            elements.append({'type':'bathing-register', 'id':attrs['OBJECTID'],
                             'center':{'lat':point[0], 'lon':point[1]},
                             'tags':{'name':attrs.get('Navn') or 'Registrert badeområde'},
                             'recordUrl':record_url, 'municipality':attrs.get('Kommunenavn')})
        return elements
    result, errors = {'lakes': [], 'places': []}, {}
    if discovery_errors:
        errors['permits'] = 'En eller flere fiskekortkilder kunne ikke kontrolleres.'
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {name: pool.submit(fn) for name, fn, enabled in
                [('lakes', lakes, include_fishing), ('places', places, include_bathing)] if enabled}
        for name, job in jobs.items():
            try:
                result[name] = job.result()
            except Exception:
                result[name] = []
                errors[name] = 'Datakilden kunne ikke hentes nå.'
    def lake_order(feature):
        geom, attrs = feature.get('geometry') or {}, feature.get('attributes') or {}
        point = nearest_point(lat, lon, [geom])
        km = 0 if contains(lat, lon, geom.get('rings', [])) else distance(lat, lon, *point) if point else float('inf')
        return (not bool(attrs.get('navn')), km)
    result['lakes'] = [f for f in result['lakes'] if lake_order(f)[1] <= radius_km]
    candidate_count = len(result['lakes'])
    # Fish presence and permit evidence remain mandatory; unknown drinking-water status is allowed.
    result['lakes'] = [f for f in result['lakes'] if approved_access((f.get('attributes') or {}).get('vatnlnr'), access_registry)]
    result['lakes'] = sorted(result['lakes'], key=lake_order)
    fish_families = {'Cyprinidae','Leuciscidae','Salmonidae','Percidae','Esocidae','Lotidae',
                     'Gadidae','Anguillidae','Osmeridae','Gasterosteidae','Cottidae',
                     'Cobitidae','Nemacheilidae','Petromyzontidae','Pleuronectidae'}
    def fish(feature):
        coords = [p for ring in feature.get('geometry', {}).get('rings', []) for p in ring]
        if not coords:
            return []
        params = {'decimalLatitude':f'{min(p[1] for p in coords)},{max(p[1] for p in coords)}',
                  'decimalLongitude':f'{min(p[0] for p in coords)},{max(p[0] for p in coords)}',
                  'hasCoordinate':'true', 'limit':300,
                  # GBIF family keys, including Cyprinidae from the confirmed karuss record.
                  'taxonKey':[7336,10307246,8615,4481,7662,9639,3701,2949,8480,7666,8621,3385,6759123,2212,8611]}
        records = []
        for offset in range(0, 3000, 300):
            data = fetch('https://api.gbif.org/v1/occurrence/search?' + urlencode({**params,'offset':offset}, doseq=True), timeout=35)
            records.extend(r for r in data.get('results', []) if r.get('species') in SPECIES or r.get('family') in fish_families)
            if data.get('endOfRecords', len(data.get('results', [])) < 300):
                return records
        raise RuntimeError('Artsoppslaget inneholder flere registreringer enn grensen.')
    result['fish'] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(fish, feature) for feature in result['lakes']] if include_species else []
        for job in jobs:
            try:
                result['fish'].extend(job.result())
            except Exception:
                errors['fish'] = 'Artsoppslaget kunne ikke hentes fullstendig.'
    waters = []
    water_geometries = {}
    for feature in result['lakes']:
        geom, attrs = feature.get('geometry') or {}, feature.get('attributes') or {}
        point = nearest_point(lat, lon, [geom])
        if not point:
            continue
        km = 0 if contains(lat, lon, geom.get('rings', [])) else distance(lat, lon, *point)
        if km > radius_km:
            continue
        name = attrs.get('navn')
        entries = {}
        for record in result['fish']:
            rlat, rlon = record.get('decimalLatitude'), record.get('decimalLongitude')
            if rlat is None or rlon is None or record.get('occurrenceStatus') == 'ABSENT':
                continue
            if not contains(rlat, rlon, geom.get('rings', [])):
                continue
            locality = (record.get('waterBody') or record.get('locality') or '').strip().casefold()
            uncertainty = record.get('coordinateUncertaintyInMeters')
            if uncertainty is not None and float(uncertainty) > 100:
                continue
            if not name or (uncertainty is None and locality != name.strip().casefold()):
                continue
            species = record.get('species') or record.get('scientificName')
            if not species:
                continue
            year = record.get('year')
            old = entries.get(species)
            entry = {'name': SPECIES.get(species, species), 'scientificName': species,
                     'year': year, 'datasetKey':record.get('datasetKey'),
                     'url': 'https://www.gbif.org/occurrence/' + str(record['key'])}
            if not old or (year or 0) > (old.get('year') or 0):
                entries[species] = entry
        evidence = approved_access(attrs.get('vatnlnr'), access_registry)
        # The checked permit source already documents this particular fishing
        # water. GBIF enriches species details; lack of observations is not
        # evidence that the water has no fish.
        if not evidence:
            continue
        permit_url, info_url = evidence['permitUrl'], evidence['permitSource']
        waters.append({'name': name or 'Vann uten registrert navn', 'named': bool(name),
                       'id': attrs.get('vatnlnr'), 'municipality': attrs.get('kommune'), 'lat': point[0], 'lon': point[1],
                       'distanceKm': round(km, 1), 'species': list(entries.values()),
                       'speciesUnavailable': 'fish' in errors, 'permitUrl': permit_url, 'fishingInfoUrl': info_url,
                       'drinkingWaterStatus':evidence.get('drinkingWaterStatus') or 'unknown_assumed_not_drinking', 'drinkingWaterSource':evidence.get('drinkingWaterSource'),
                       'accessCheckedOn':evidence['checkedOn']})
        water_geometries[attrs.get('vatnlnr')] = geom
        if include_geometry:
            waters[-1]['geometry'] = geom
    waters.sort(key=lambda w: (not w['named'], w['distanceKm']))
    bathing = []
    for element in result['places']:
        tags = element.get('tags', {})
        if tags.get('leisure') == 'fishing' or tags.get('access') in ('private', 'no') or tags.get('swimming') == 'no':
            continue
        point = element if element['type'] == 'node' else element.get('center', {})
        if point.get('lat') is None or point.get('lon') is None:
            continue
        km = distance(lat, lon, point['lat'], point['lon'])
        if km > bath_radius_km:
            continue
        facilities = []
        for key, label in [('toilets', 'Toalett'), ('wheelchair', 'Rullestoltilgang'), ('shower', 'Dusj')]:
            if tags.get(key) == 'yes':
                facilities.append(label)
        if tags.get('natural') == 'beach':
            facilities.append('Strand')
        # A shared name is insufficient: also require the bathing location to
        # be in, or within 500 m of, that specific NVE lake polygon.
        matches = []
        for water in waters:
            if not water['named'] or water['name'].strip().casefold() != tags.get('name', '').strip().casefold():
                continue
            geom = water_geometries.get(water['id'], {})
            nearest = nearest_point(point['lat'], point['lon'], [geom])
            separation = 0 if contains(point['lat'], point['lon'], geom.get('rings', [])) else distance(point['lat'], point['lon'], *nearest) if nearest else float('inf')
            if separation <= .5:
                matches.append((separation, water))
        matching_water = min(matches, key=lambda entry: entry[0])[1] if matches else None
        bathing.append({'name': tags.get('name') or 'Kartlagt badeplass / strand',
                        'lat': point['lat'], 'lon': point['lon'], 'distanceKm': round(km, 1),
                        'facilities': facilities, 'website': safe_url(tags.get('website') or tags.get('contact:website')),
                        'recordUrl': element.get('recordUrl') or f'https://www.openstreetmap.org/{element["type"]}/{element["id"]}',
                        'municipality':element.get('municipality'),
                        'species':matching_water['species'] if matching_water else [],
                        'speciesUnavailable':'fish' in errors,
                        'fishWaterName':matching_water['name'] if matching_water else None})
    bathing.sort(key=lambda b: b['distanceKm'])
    unique = []
    for item in bathing:
        if not any(item['name'] == other['name'] and distance(item['lat'], item['lon'], other['lat'], other['lon']) < .2 for other in unique):
            unique.append(item)
    return {'waters': waters, 'bathing': unique, 'radiusKm': radius_km, 'bathingRadiusKm':bath_radius_km, 'errors': errors,
            'fishingCriteria':'documented_fishing_water+not_known_drinking_water+permit', 'fishingCoverage':'documented_permit_sources', 'candidateCount':candidate_count}
