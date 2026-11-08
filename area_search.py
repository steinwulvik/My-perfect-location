"""Complete scoped address retrieval and reusable spatial data for area discovery.

Results describe inspected address points, never whole streets or properties.
"""
import math
import re
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor


def municipalities(fetch):
    rows = fetch('https://api.kartverket.no/kommuneinfo/v1/kommuner?sorter=kommunenavn')
    if not isinstance(rows, list):
        raise RuntimeError('Kommunelisten kunne ikke hentes fra Kartverket.')
    return [{'code': x['kommunenummer'], 'name': x.get('kommunenavnNorsk') or x['kommunenavn']}
            for x in rows if re.fullmatch(r'\d{4}', str(x.get('kommunenummer', '')))]


def places(fetch, code):
    """Active settlement and locality names, excluding address/object names."""
    if not re.fullmatch(r'\d{4}', code):
        raise ValueError('Velg en kommune.')
    result, seen, signatures, maximum_reported = [], set(), set(), 0
    for page in range(1, 501):
        data = fetch('https://ws.geonorge.no/stedsnavn/v1/sted?' + urlencode({
            'knr':code, 'treffPerSide':500, 'side':page, 'utkoordsys':4258}), timeout=30)
        rows, total = data.get('navn'), data.get('metadata', {}).get('totaltAntallTreff')
        if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise RuntimeError('Stedsnavnlisten kunne ikke hentes fullstendig.')
        maximum_reported = max(maximum_reported, total)
        # SSR collapses alternative names per place within each page, not
        # globally. The same place may therefore occur on several pages, and
        # a short page/metadata count is not proof of the end of the catalogue.
        if not rows:
            if (page-1)*500 < maximum_reported:
                raise RuntimeError('Stedsnavntjenesten mangler en forventet resultatside.')
            # SSR contains several objects with the same spelling (for
            # example a settlement, road name, school and watercourse).
            # Keep one representative point per normalized display name,
            # preferring place-like object types and entries with coordinates.
            preferred_types = {'By':0, 'Tettsted':1, 'Bydel':2, 'Grend':3,
                               'Bygd':4, 'Bygdelag':5, 'Boligfelt':6,
                               'Tettbebyggelse':7}
            unique = {}
            for item in result:
                key = ' '.join(item['name'].casefold().split())
                score = (preferred_types.get(item['type'], 20),
                         item['lat'] is None or item['lon'] is None,
                         len(item['name']), item['id'])
                previous = unique.get(key)
                if previous is None or score < previous[0]:
                    unique[key] = (score, item)
            return [item for _, item in unique.values()]
        signature = repr(rows)
        if signature in signatures:
            raise RuntimeError('Stedsnavntjenesten gjentar samme resultatside.')
        signatures.add(signature)
        for row in rows:
            identity = str(row.get('stedsnummer', ''))
            if not identity.isdigit():
                raise RuntimeError('Stedsnavntjenesten returnerte en ugyldig oppføring.')
            if identity in seen: continue
            seen.add(identity)
            if row.get('stedstatus') != 'aktiv': continue
            if not any(str(k.get('kommunenummer')) == code for k in row.get('kommuner', [])): continue
            place_type = row.get('navneobjekttype')
            if place_type not in {'By', 'Tettsted', 'Bydel', 'Grend', 'Bygd',
                                  'Bygdelag', 'Boligfelt', 'Tettbebyggelse'}:
                continue
            names = row.get('stedsnavn') or []
            preferred = next((n for n in names if n.get('skrivemåtestatus') == 'godkjent og prioritert'), None)
            preferred = preferred or next((n for n in names if n.get('skrivemåte')), None)
            if not preferred: continue
            point = row.get('representasjonspunkt') or {}
            try:
                lat, lon = float(point['nord']), float(point['øst'])
                if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                lat = lon = None
            result.append({'id':identity, 'name':preferred['skrivemåte'],
                           'type':place_type, 'lat':lat, 'lon':lon})
    raise RuntimeError('Stedsnavnlisten er for stor til å hentes fullstendig.')


def place_candidates(fetch, code, place, radius_km, postal_code='', street=''):
    """All registered addresses in a visible circle, clipped to municipality."""
    if not re.fullmatch(r'\d{4}', code) or not .5 <= radius_km <= 20:
        raise ValueError('Velg kommune og en områderadius mellom 0,5 og 20 km.')
    if place.get('lat') is None or place.get('lon') is None:
        raise ValueError('Stedet mangler kartpunkt og kan ikke avgrenses.')
    if postal_code and not re.fullmatch(r'\d{4}', postal_code):
        raise ValueError('Postnummer må ha fire sifre.')
    if len(street) > 120 or '*' in street:
        raise ValueError('Skriv et helt gatenavn uten stjerne.')
    lat, lon = place['lat'], place['lon']
    filtered = bool(postal_code or street)
    params = {'treffPerSide':1000, 'utkoordsys':4258}
    if filtered:
        endpoint = 'sok'
        params.update(kommunenummer=code, objtype='Vegadresse')
        if postal_code: params['postnummer'] = postal_code
        if street: params['adressenavn'] = street
    else:
        endpoint = 'punktsok'
        params.update(lat=lat, lon=lon, radius=round(radius_km*1000), koordsys=4258)
    def page(index):
        data = fetch('https://ws.geonorge.no/adresser/v1/'+endpoint+'?'+urlencode({**params, 'side':index}), timeout=30)
        rows, total = data.get('adresser'), data.get('metadata', {}).get('totaltAntallTreff')
        if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise RuntimeError('Adressegrunnlaget kunne ikke kontrolleres.')
        return rows, total
    rows, total = page(0)
    result = {'mode':'place-addresses-v3', 'candidates':[], 'totalAddresses':0, 'sourceTotalAddresses':total,
              'complete':False, 'locality':place['name'], 'placeId':place['id'],
              'scopeRadiusKm':radius_km, 'scopeCenter':{'lat':lat,'lon':lon}, 'postalCode':postal_code,
              'municipalityCode':code, 'skippedAddresses':0, 'duplicateRecords':0}
    if total >= 10000:
        result.update(tooLarge=True, totalIsLowerBound=True)
        return result
    signatures = {repr(rows)}
    for index in range(1, math.ceil(total/1000)):
        batch, count = page(index)
        if count != total or not batch or repr(batch) in signatures:
            raise RuntimeError('Adressetjenesten returnerte en endret, gjentatt eller manglende side.')
        signatures.add(repr(batch)); rows.extend(batch)
    seen = set()
    for row in rows:
        if str(row.get('kommunenummer')) != code: continue
        if postal_code and str(row.get('postnummer')) != postal_code: continue
        if street and str(row.get('adressenavn') or '').casefold() != street.casefold(): continue
        point = row.get('representasjonspunkt') or {}
        try:
            rlat, rlon = float(point['lat']), float(point['lon'])
            if not (math.isfinite(rlat) and math.isfinite(rlon) and -90 <= rlat <= 90 and -180 <= rlon <= 180): raise ValueError()
        except (KeyError, TypeError, ValueError):
            result['skippedAddresses'] += 1; continue
        rad = math.pi/180
        h = math.sin((rlat-lat)*rad/2)**2 + math.cos(lat*rad)*math.cos(rlat*rad)*math.sin((rlon-lon)*rad/2)**2
        distance = 6371*2*math.atan2(math.sqrt(h), math.sqrt(max(0,1-h)))
        if distance > radius_km+.001: continue
        if not row.get('adressenavn') or not row.get('adressetekst'):
            result['skippedAddresses'] += 1; continue
        identity = (row['adressetekst'], str(row.get('postnummer')), rlat, rlon)
        if identity in seen:
            result['duplicateRecords'] += 1; continue
        seen.add(identity)
        result['candidates'].append({'name':', '.join(filter(None,[row['adressetekst'],
            ' '.join(filter(None,[str(row.get('postnummer') or ''),row.get('poststed')])),row.get('kommunenavn')])),
            'street':row['adressenavn'], 'locality':place['name'], 'postalCode':str(row.get('postnummer') or ''),
            'municipality':row.get('kommunenavn') or code, 'lat':rlat, 'lon':rlon})
    result['loadedAddresses'] = len(rows)
    result['totalAddresses'] = len(result['candidates'])+result['skippedAddresses']+result['duplicateRecords']
    result['complete'] = len(rows)==total and not result['skippedAddresses'] and not result['duplicateRecords']
    return result


def candidates(fetch, code, locality, postal_code='', street=''):
    """Read every page in a scoped address search; never choose sample points."""
    locality = str(locality).strip()
    postal_code, street = str(postal_code).strip(), str(street).strip()
    if not re.fullmatch(r'\d{4}', code) or not 2 <= len(locality) <= 80:
        raise ValueError('Velg kommune og skriv et poststed.')
    if postal_code and not re.fullmatch(r'\d{4}', postal_code):
        raise ValueError('Postnummer må ha fire sifre.')
    if '*' in locality or '*' in street:
        raise ValueError('Skriv et helt poststed eller gatenavn uten stjerne.')
    if len(street) > 120:
        raise ValueError('Gatenavnet er for langt.')
    params = {'kommunenummer': code, 'poststed': locality, 'objtype': 'Vegadresse',
              'treffPerSide': 1000, 'utkoordsys': 4258}
    if postal_code: params['postnummer'] = postal_code
    if street: params['adressenavn'] = street
    def page(index):
        data = fetch('https://ws.geonorge.no/adresser/v1/sok?' + urlencode({**params, 'side': index}), timeout=30)
        if not isinstance(data.get('adresser'), list):
            raise RuntimeError('Adressene kunne ikke hentes fra Kartverket.')
        total = data.get('metadata', {}).get('totaltAntallTreff')
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise RuntimeError('Kartverket mangler antall adresser. Fullstendighet kan ikke kontrolleres.')
        return data, total
    first, total = page(0)
    result = {'mode':'all-addresses-v2', 'candidates':[], 'totalAddresses':total,
              'loadedAddresses':0, 'complete':False, 'municipalityCode':code,
              'locality':locality, 'postalCode':postal_code, 'street':street,
              'skippedAddresses':0, 'duplicateRecords':0, 'warnings':[]}
    # 10,000 can be a service cap, not a true count. Require narrower scope
    # instead of returning a hidden sample and pretending it is complete.
    if total >= 10000:
        result.update(tooLarge=True, totalIsLowerBound=True)
        result['warnings'].append('Minst 10 000 adresser. Avgrens med postnummer eller gate for å undersøke alle.')
        return result
    rows = first['adresser'][:]
    signatures = {repr(first['adresser'])}
    for index in range(1, math.ceil(total / 1000)):
        data, current_total = page(index)
        if current_total != total:
            raise RuntimeError('Adressegrunnlaget endret seg under henting. Prøv søket på nytt.')
        signature = repr(data['adresser'])
        if signature in signatures or not data['adresser']:
            raise RuntimeError('Kartverket returnerte en gjentatt eller manglende side. Prøv søket på nytt.')
        signatures.add(signature)
        rows.extend(data['adresser'])
    result['loadedAddresses'] = len(rows)
    seen = set()
    for row in rows:
        point = row.get('representasjonspunkt') or {}
        try:
            lat, lon = float(point['lat']), float(point['lon'])
        except (KeyError, TypeError, ValueError):
            result['skippedAddresses'] += 1
            continue
        valid = (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
                 and str(row.get('kommunenummer', '')) == code and row.get('adressenavn') and row.get('adressetekst')
                 and str(row.get('poststed', '')).casefold() == locality.casefold()
                 and (not postal_code or str(row.get('postnummer', '')) == postal_code)
                 and (not street or str(row.get('adressenavn', '')).casefold() == street.casefold()))
        if not valid:
            result['skippedAddresses'] += 1
            continue
        identity = (row.get('adressetekst'), str(row.get('postnummer')), lat, lon)
        if identity in seen:
            result['duplicateRecords'] += 1
            continue
        seen.add(identity)
        result['candidates'].append({
            'name': ', '.join(filter(None, [row.get('adressetekst'),
                ' '.join(filter(None, [str(row.get('postnummer') or ''), row.get('poststed')])), row.get('kommunenavn')])),
            'street':row['adressenavn'], 'locality':row['poststed'], 'postalCode':str(row.get('postnummer') or ''),
            'municipality':row.get('kommunenavn') or code, 'lat':lat, 'lon':lon})
    result['candidates'].sort(key=lambda p:(p['street'].casefold(), p['name'].casefold()))
    result['complete'] = len(rows) == total and not result['skippedAddresses'] and not result['duplicateRecords']
    if not result['complete']:
        result['warnings'].append('Adressegrunnlaget er ufullstendig. Resultatet gjelder bare adressene som kunne hentes og stedfestes.')
    return result


def nearby(fetch, lat, lon, radius):
    from nearby_data import ENDPOINTS
    area = f'(around:{radius},{lat},{lon})'
    query = ('[out:json][timeout:30];(nwr'+area+'[amenity~"^(school|kindergarten)$"];'
             'nwr'+area+'[shop~"^(supermarket|convenience|greengrocer)$"];);out center;')
    for endpoint in ENDPOINTS:
        try:
            data = fetch(endpoint+'?'+urlencode({'data': query}), timeout=40)
            if data.get('remark') or not isinstance(data.get('elements'), list):
                raise RuntimeError('Ufullstendig kartoppslag')
            return data['elements']
        except Exception:
            continue
    raise RuntimeError('Nærtilbud kunne ikke hentes fullstendig.')


def context(fetch, lat, lon, radius, use_nearby, use_fish, use_bath, helpers):
    result = {'elements': [], 'waters': [], 'bathing': [], 'errors': {}, 'radiusKm': radius,
              'fishingComplete': False}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {}
        if use_nearby:
            jobs['nearby'] = pool.submit(nearby, fetch, lat, lon, int(radius*1000))
        if use_fish or use_bath:
            from water_data import load_waters
            jobs['waters'] = pool.submit(load_waters, fetch, lat, lon, *helpers,
                radius_km=radius if use_fish else 1, bath_radius_km=radius if use_bath else 1,
                include_species=False, include_geometry=True,
                include_fishing=use_fish, include_bathing=use_bath)
        for key, job in jobs.items():
            try:
                data = job.result()
                if key == 'nearby':
                    result['elements'] = data
                else:
                    result.update({k: data[k] for k in ('waters', 'bathing')})
                    result['errors'].update(data.get('errors', {}))
            except Exception as exc:
                result['errors'][key] = str(exc)
    return result
