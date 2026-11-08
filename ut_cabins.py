"""Read UT.no's cabin catalogue; never treat an incomplete page as a full list."""
import json
import math
import os

UT_GRAPHQL = os.environ.get('UT_GRAPHQL_URL',
    'https://ut.no/api/graphql')


def point_from_geojson(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('Hyttegeometri mangler')
    if value.get('type') == 'Feature':
        value = value.get('geometry') or {}
    if value.get('type') != 'Point':
        raise ValueError('Hyttegeometri er ikke et punkt')
    coords = value.get('coordinates', [])
    if len(coords) < 2 or any(isinstance(v, bool) for v in coords[:2]):
        raise ValueError('Hyttekoordinater mangler')
    lon, lat = float(coords[0]), float(coords[1])
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Ugyldige hyttekoordinater')
    return lat, lon


def load_ut_catalog(fetch):
    result, seen_ids, cursors = [], set(), set()
    cursor = None
    expected = None
    while True:
        after = ',after:'+json.dumps(cursor) if cursor is not None else ''
        query = ('{cabins(paging:{first:100'+after+'}){totalCount '
                 'pageInfo{hasNextPage endCursor} edges{node{id name dntCabin geojson}}}}')
        response = fetch(UT_GRAPHQL, method='POST', data={'query': query},
                         headers={'Origin': 'https://ut.no', 'Referer': 'https://ut.no/'}, timeout=30)
        if not isinstance(response, dict) or response.get('errors'):
            raise RuntimeError('UT.no returnerte en API-feil')
        connection = (response.get('data') or {}).get('cabins')
        if not isinstance(connection, dict) or not isinstance(connection.get('edges'), list):
            raise RuntimeError('UT.no returnerte et ukjent hytteformat')
        total = connection.get('totalCount')
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise RuntimeError('UT.no mangler antall hytter i katalogen')
        if expected is None:
            expected = total
        elif expected != total:
            raise RuntimeError('UT.no-katalogen endret seg under innlasting. Prøv igjen.')
        info = connection.get('pageInfo')
        if not isinstance(info, dict) or not isinstance(info.get('hasNextPage'), bool):
            raise RuntimeError('UT.no mangler pagineringsinformasjon')
        for edge in connection['edges']:
            row = edge.get('node') if isinstance(edge, dict) else None
            if not isinstance(row, dict) or not str(row.get('id', '')).isdigit() or not isinstance(row.get('dntCabin'), bool):
                raise RuntimeError('UT.no returnerte ufullstendige hytteopplysninger')
            identity = str(row['id'])
            if identity not in seen_ids:
                seen_ids.add(identity)
                result.append(row)
        if not info['hasNextPage']:
            if len(seen_ids) != expected:
                raise RuntimeError('UT.no-katalogen er ufullstendig')
            break
        following = info.get('endCursor')
        if not isinstance(following, str) or not following or following in cursors or not connection['edges']:
            raise RuntimeError('UT.no-pagineringen stoppet før siste side')
        cursors.add(following)
        cursor = following
    if not result:
        raise RuntimeError('UT.no-katalogen er tom; fullstendighet kan ikke bekreftes')
    # A broken geometry on a DNT hut must not silently produce a complete zero-hit response.
    for row in result:
        if row['dntCabin']:
            point_from_geojson(row.get('geojson'))
            if not isinstance(row.get('name'), str) or not row['name'].strip():
                raise RuntimeError('UT.no returnerte en DNT-hytte uten navn')
    return result
